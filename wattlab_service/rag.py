"""
rag.py — RAG measurement module for WattLab.

Three inference modes on a free-text question:
  baseline   — cold LLM inference, no retrieval
  rag        — embed query → ChromaDB top_k=3 → prepend chunks → infer
  rag_large  — same but top_k=8

Embedding model and ChromaDB collection are singletons: loaded once at
first use, held for the lifetime of the FastAPI process.

Index building is separate from measurement. Call build_index() once
(or trigger via /rag/build-index endpoint). The ChromaDB store persists
to disk so subsequent restarts are fast.
"""

import asyncio
import functools
import json
import subprocess
import time
import urllib.request
from pathlib import Path

import settings as cfg
from power import get_power_watts
LOCK_FILE = Path("/tmp/gos-measure.lock")

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "all-MiniLM-L6-v2"
COLLECTION_NAME = "rag_corpus"
CHUNK_SIZE_CHARS = 512 * 4   # ~512 tokens at ~4 chars/token
OVERLAP_CHARS    = 64  * 4

MODELS = {
    "tinyllama":  {"label": "TinyLlama",   "size": "637MB", "params": "1.1B"},
    "mistral":    {"label": "Mistral 7B",  "size": "4.4GB", "params": "7B"},
    "gemma3:12b": {"label": "Gemma 3 12B", "size": "8.1GB", "params": "12B"},
    "phi4":       {"label": "Phi-4",       "size": "9.1GB", "params": "14B"},
}

TOP_K = {"baseline": 0, "rag": 3, "rag_large": 8}

SYSTEM_PROMPT = (
    "You are an expert analyst in energy consumption, sustainability, streaming media, "
    "and network infrastructure. Answer questions accurately and cite your sources where possible."
)
RETRIEVAL_PROMPT = "Here are relevant excerpts from the corpus:\n\n{chunks}\n\n---\nQuestion: {question}"
BASELINE_PROMPT  = "Question: {question}"

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_embed_model   = None
_collection    = None

# Index build status — read by /rag/index-status endpoint
index_status = "unknown"   # "unknown" | "building" | "ready" | "error"
index_error  = None
index_doc_count = 0


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(EMBED_MODEL)
    return _embed_model


def _get_collection(chroma_path: str):
    """Return the persistent ChromaDB collection (create if not exists)."""
    global _collection
    if _collection is not None:
        return _collection
    import chromadb
    from chromadb.config import Settings
    client = chromadb.PersistentClient(
        path=chroma_path,
        settings=Settings(anonymized_telemetry=False),
    )
    _collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    return _collection


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

def _load_pdfs(corpus_path: Path) -> list[dict]:
    from pypdf import PdfReader
    pages = []
    for pdf_path in sorted(corpus_path.rglob("*.pdf")):
        try:
            reader = PdfReader(str(pdf_path))
            for page_num, page in enumerate(reader.pages):
                text = (page.extract_text() or "").strip()
                if text:
                    pages.append({"text": text, "source": pdf_path.name, "page": page_num})
        except Exception as e:
            pass  # warn-only: one AES-encrypted PDF won't block the rest
    return pages


def _chunk_pages(pages: list[dict]) -> list[dict]:
    chunks, idx = [], 0
    for page in pages:
        text, start = page["text"], 0
        while start < len(text):
            body = text[start:min(start + CHUNK_SIZE_CHARS, len(text))].strip()
            if body:
                chunks.append({"text": body, "source": page["source"],
                                "page": page["page"], "chunk_index": idx})
                idx += 1
            start += CHUNK_SIZE_CHARS - OVERLAP_CHARS
    return chunks


def build_index(rebuild: bool = False):
    """Build (or rebuild) the ChromaDB index from the corpus PDFs.
    Runs synchronously — call from a thread to avoid blocking the event loop.
    Updates module-level index_status throughout.
    """
    global index_status, index_error, index_doc_count, _collection

    s = cfg.load()
    corpus_path = Path(s["rag_corpus_path"])
    chroma_path = s["rag_chroma_path"]

    try:
        index_status = "building"

        collection = _get_collection(chroma_path)
        existing = collection.count()

        if existing > 0 and not rebuild:
            index_doc_count = existing
            index_status = "ready"
            return

        if rebuild and existing > 0:
            import chromadb
            from chromadb.config import Settings
            client = chromadb.PersistentClient(
                path=chroma_path,
                settings=Settings(anonymized_telemetry=False),
            )
            client.delete_collection(COLLECTION_NAME)
            _collection = client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            collection = _collection

        pages  = _load_pdfs(corpus_path)
        chunks = _chunk_pages(pages)

        model = _get_embed_model()
        texts     = [c["text"]              for c in chunks]
        ids       = [str(c["chunk_index"])  for c in chunks]
        metadatas = [{"source": c["source"], "page": c["page"],
                      "chunk_index": c["chunk_index"]} for c in chunks]

        batch_size = 256
        for i in range(0, len(texts), batch_size):
            embeddings = model.encode(texts[i:i+batch_size],
                                      show_progress_bar=False).tolist()
            collection.add(
                documents=texts[i:i+batch_size],
                embeddings=embeddings,
                ids=ids[i:i+batch_size],
                metadatas=metadatas[i:i+batch_size],
            )

        index_doc_count = collection.count()
        index_status = "ready"

    except Exception as e:
        index_status = "error"
        index_error  = str(e)
        raise


def check_index():
    """Fast check: is the persistent index already populated? Updates index_status."""
    global index_status, index_doc_count
    s = cfg.load()
    try:
        collection = _get_collection(s["rag_chroma_path"])
        count = collection.count()
        if count > 0:
            index_doc_count = count
            index_status = "ready"
        else:
            index_status = "not_built"
    except Exception:
        index_status = "not_built"


def corpus_list() -> list[dict]:
    """List PDFs in the corpus, marking which are indexed.

    Indexed status is derived from the source filenames present in the
    ChromaDB collection metadata — chunks store {"source": pdf_path.name}
    so a PDF is "indexed" if its filename appears at least once.
    """
    s = cfg.load()
    corpus_path = Path(s["rag_corpus_path"])
    indexed = set()
    try:
        collection = _get_collection(s["rag_chroma_path"])
        # Fetch all metadatas; corpus is small (~100 docs, ~5–10k chunks).
        result = collection.get(limit=200000, include=["metadatas"])
        for meta in result.get("metadatas") or []:
            if meta and meta.get("source"):
                indexed.add(meta["source"])
    except Exception:
        pass
    docs = []
    if not corpus_path.exists():
        return docs
    for pdf_path in sorted(corpus_path.rglob("*.pdf")):
        try:
            size_kb = pdf_path.stat().st_size // 1024
        except Exception:
            size_kb = 0
        try:
            rel = str(pdf_path.relative_to(corpus_path))
        except ValueError:
            rel = pdf_path.name
        docs.append({
            "name": pdf_path.name,
            "rel_path": rel,
            "size_kb": size_kb,
            "indexed": pdf_path.name in indexed,
        })
    return docs


async def measure_baseline(polls: int = 10) -> float:
    readings = []
    for _ in range(polls):
        readings.append(await get_power_watts())
        await asyncio.sleep(1)
    return round(sum(readings) / len(readings), 2)


async def poll_during_task(stop_event: asyncio.Event) -> list:
    readings = []
    while not stop_event.is_set():
        readings.append((time.time(), await get_power_watts()))
        await asyncio.sleep(1)
    return readings


def read_sensors() -> dict:
    try:
        result = subprocess.run(["sensors", "-j"], capture_output=True, text=True)
        data = json.loads(result.stdout)
        return {
            "cpu_tctl":     data["k10temp-pci-00c3"]["Tctl"]["temp1_input"],
            "gpu_junction": data["amdgpu-pci-0300"]["junction"]["temp2_input"],
        }
    except Exception:
        return {"cpu_tctl": None, "gpu_junction": None}


def unload_model(model_key: str):
    payload = json.dumps({"model": model_key, "keep_alive": 0}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def confidence(delta_w: float, poll_count: int, w_base: float) -> dict:
    s = cfg.load()
    noise_w = s["variance_pct"] / 100.0 * max(w_base, 1.0)
    if delta_w > s["variance_green_x"] * noise_w and poll_count >= s["conf_green_polls"]:
        return {"flag": "🟢", "label": "Repeatable"}
    elif delta_w >= s["variance_yellow_x"] * noise_w or poll_count >= s["conf_yellow_polls"]:
        return {"flag": "🟡", "label": "Early insight"}
    else:
        return {"flag": "🔴", "label": "Need more data"}


# ---------------------------------------------------------------------------
# RAG query (synchronous — called via run_in_executor)
# ---------------------------------------------------------------------------

def _run_rag_query(model_key: str, rag_mode: str, question: str,
                   on_token=None, jobs: dict = None, job_id: str = None) -> dict:
    """
    Full RAG pipeline: embed → retrieve → build prompt → infer.
    Returns timing dict with all metrics. Runs in a thread.
    """
    s = cfg.load()
    top_k = TOP_K[rag_mode]

    # --- Embedding & retrieval ---
    embedding_ms  = 0.0
    retrieval_ms  = 0.0
    chunks_retrieved = 0
    chunk_sources = []

    if top_k > 0:
        embed_model = _get_embed_model()

        t0 = time.perf_counter()
        query_embedding = embed_model.encode([question])[0].tolist()
        embedding_ms = (time.perf_counter() - t0) * 1000

        collection = _get_collection(s["rag_chroma_path"])
        t1 = time.perf_counter()
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas"],
        )
        retrieval_ms = (time.perf_counter() - t1) * 1000

        docs      = results["documents"][0] if results["documents"] else []
        metadatas = results["metadatas"][0]  if results["metadatas"]  else []
        chunk_texts = []
        for doc, meta in zip(docs, metadatas):
            chunk_texts.append(f"[{meta.get('source','?')} — page {meta.get('page','?')}]\n{doc}")
            chunk_sources.append(meta.get("source", "?"))

        chunks_retrieved = len(chunk_texts)
        joined = "\n\n".join(chunk_texts)
        user_message = RETRIEVAL_PROMPT.format(chunks=joined, question=question)
    else:
        user_message = BASELINE_PROMPT.format(question=question)

    # --- LLM inference (streaming) ---
    num_ctx = 8192 if rag_mode == "rag_large" else 4096
    payload = json.dumps({
        "model": model_key,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "stream": True,
        "options": {"num_ctx": num_ctx},
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )

    full_response  = ""
    input_tokens   = 0
    output_tokens  = 0
    t_infer_start  = time.time()

    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw_line in resp:
            line = raw_line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            if chunk.get("done"):
                input_tokens  = chunk.get("prompt_eval_count", 0)
                output_tokens = chunk.get("eval_count", 0)
                break
            token = chunk.get("message", {}).get("content", "")
            if token:
                full_response += token
                if on_token:
                    on_token(token)
                if jobs and job_id:
                    jobs[job_id]["partial_response"] = full_response

    inference_ms = (time.time() - t_infer_start) * 1000
    duration_s   = round(inference_ms / 1000, 2)
    tokens_per_sec = round(output_tokens / duration_s, 1) if duration_s > 0 else 0.0

    return {
        "rag_mode":        rag_mode,
        "question":        question,
        "answer":          full_response,
        "top_k":           top_k,
        "chunk_size":      512,
        "chunks_retrieved": chunks_retrieved,
        "chunk_sources":   list(dict.fromkeys(chunk_sources)),  # unique, ordered
        "embedding_ms":    round(embedding_ms, 1),
        "retrieval_ms":    round(retrieval_ms, 1),
        "inference_ms":    round(inference_ms, 1),
        "input_tokens":    input_tokens,
        "output_tokens":   output_tokens,
        "tokens_per_sec":  tokens_per_sec,
        "duration_s":      duration_s,
        "num_ctx":         num_ctx,
    }


# ---------------------------------------------------------------------------
# Main measurement (async, wraps P110 protocol)
# ---------------------------------------------------------------------------

async def run_rag_measurement(model_key: str, rag_mode: str, question: str,
                               jobs: dict = None, job_id: str = None) -> dict:
    model = MODELS[model_key]
    s = cfg.load()

    if jobs and job_id:
        jobs[job_id]["stage"] = "baseline"
        jobs[job_id]["partial_response"] = ""

    unload_model(model_key)
    await asyncio.sleep(s["llm_unload_settle_s"])
    w_base = await measure_baseline(polls=s["baseline_polls"])
    sensors_base = read_sensors()

    if jobs and job_id:
        jobs[job_id]["stage"] = "inference"

    LOCK_FILE.write_text(job_id or "rag")
    stop_event = asyncio.Event()
    poll_task  = asyncio.create_task(poll_during_task(stop_event))

    fn = functools.partial(_run_rag_query, model_key, rag_mode, question,
                           None, jobs, job_id)
    query_result = await asyncio.get_event_loop().run_in_executor(None, fn)

    stop_event.set()
    readings = await poll_task
    LOCK_FILE.unlink(missing_ok=True)
    sensors_end = read_sensors()

    duration_s    = query_result["duration_s"]
    output_tokens = query_result["output_tokens"]
    w_task   = sum(r[1] for r in readings) / len(readings) if readings else w_base
    delta_w  = round(w_task - w_base, 2)
    delta_e_wh = round(delta_w * (duration_s / 3600), 4)
    mwh_per_token = round((delta_e_wh * 1000) / max(output_tokens, 1), 4) \
        if output_tokens else None
    conf = confidence(delta_w, len(readings), w_base)

    if jobs and job_id:
        jobs[job_id]["stage"] = "done"

    return {
        "mode":         "rag",
        "rag_mode":     rag_mode,
        "model_key":    model_key,
        "model_label":  model["label"],
        "model_params": model["params"],
        "question":     query_result["question"],
        "answer":       query_result["answer"],
        "top_k":        query_result["top_k"],
        "chunk_size":   query_result["chunk_size"],
        "chunks_retrieved": query_result["chunks_retrieved"],
        "chunk_sources":    query_result["chunk_sources"],
        "embedding_ms": query_result["embedding_ms"],
        "retrieval_ms": query_result["retrieval_ms"],
        "num_ctx":      query_result["num_ctx"],
        "inference": {
            "output_tokens": output_tokens,
            "input_tokens":  query_result["input_tokens"],
            "tokens_per_sec": query_result["tokens_per_sec"],
            "duration_s":    duration_s,
            "response":      query_result["answer"],
        },
        "energy": {
            "w_base":       round(w_base, 2),
            "w_task":       round(w_task, 2),
            "delta_w":      delta_w,
            "delta_t_s":    duration_s,
            "delta_e_wh":   delta_e_wh,
            "mwh_per_token": mwh_per_token,
            "poll_count":   len(readings),
            "confidence":   conf,
        },
        "thermals": {
            "cpu_base": sensors_base.get("cpu_tctl"),
            "gpu_base": sensors_base.get("gpu_junction"),
            "cpu_end":  sensors_end.get("cpu_tctl"),
            "gpu_end":  sensors_end.get("gpu_junction"),
        },
        "scope": "Device layer only (GoS1). Network and CPE excluded. No amortised training cost.",
    }
