#!/usr/bin/env python3
"""
rag_experiment.py — RAG measurement prototype for energy/sustainability/streaming/networks corpus.

Modes:
  baseline   — cold LLM inference, no retrieval
  rag        — embed query → vector search top_k=3 → prepend chunks → infer
  rag_large  — same but top_k=8

Uses a local Ollama instance (default: http://localhost:11434).
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Lazy imports (fail fast with a helpful message if a package is missing)
# ---------------------------------------------------------------------------

def _require(pkg, import_name=None):
    import importlib
    name = import_name or pkg
    try:
        return importlib.import_module(name)
    except ImportError:
        print(f"[error] Missing package '{pkg}'. Run: pip install {pkg}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE_CHARS = 512 * 4   # ~512 tokens at ~4 chars/token
OVERLAP_CHARS    = 64  * 4
EMBED_MODEL      = "all-MiniLM-L6-v2"
CHROMA_DIR       = ".chroma"
RESULTS_DIR      = Path("./results/rag")
COLLECTION_NAME  = "rag_corpus"

TOP_K = {
    "baseline":  0,
    "rag":       3,
    "rag_large": 8,
}

RETRIEVAL_PROMPT = (
    "Here are relevant excerpts from the corpus:\n\n"
    "{chunks}\n\n"
    "---\n"
    "Question: {question}"
)

BASELINE_PROMPT = "Question: {question}"

SYSTEM_PROMPT = (
    "You are an expert analyst in energy consumption, sustainability, streaming media, "
    "and network infrastructure. Answer questions accurately and cite your sources where possible."
)


# ---------------------------------------------------------------------------
# PDF loading & chunking
# ---------------------------------------------------------------------------

def load_pdfs(corpus_path: Path) -> list[dict]:
    """Load all PDFs recursively; return list of page dicts."""
    pypdf = _require("pypdf")
    from pypdf import PdfReader

    pdf_files = sorted(corpus_path.rglob("*.pdf"))
    print(f"Loading corpus from {corpus_path}... {len(pdf_files)} files found")

    if not pdf_files:
        print("[error] No PDF files found in the corpus directory.", file=sys.stderr)
        sys.exit(1)

    pages = []
    for pdf_path in pdf_files:
        try:
            reader = PdfReader(str(pdf_path))
            for page_num, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                text = text.strip()
                if text:
                    pages.append({
                        "text":     text,
                        "source":   pdf_path.name,
                        "page":     page_num,
                    })
        except Exception as e:
            print(f"  [warn] Could not read {pdf_path.name}: {e}", file=sys.stderr)

    return pages


def chunk_pages(pages: list[dict]) -> list[dict]:
    """Split page texts into overlapping chunks."""
    chunks = []
    chunk_idx = 0
    for page in pages:
        text  = page["text"]
        start = 0
        while start < len(text):
            end  = min(start + CHUNK_SIZE_CHARS, len(text))
            body = text[start:end].strip()
            if body:
                chunks.append({
                    "text":        body,
                    "source":      page["source"],
                    "page":        page["page"],
                    "chunk_index": chunk_idx,
                })
                chunk_idx += 1
            start += CHUNK_SIZE_CHARS - OVERLAP_CHARS
    return chunks


# ---------------------------------------------------------------------------
# ChromaDB helpers
# ---------------------------------------------------------------------------

def get_chroma_collection(chroma_dir: str, rebuild: bool, chunks: list[dict] | None = None):
    """Return (collection, was_cached).

    If the collection already has documents and rebuild=False, skip embedding.
    """
    chromadb = _require("chromadb")
    import chromadb as _chromadb
    from chromadb.config import Settings

    client = _chromadb.PersistentClient(
        path=chroma_dir,
        settings=Settings(anonymized_telemetry=False),
    )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    existing_count = collection.count()

    if existing_count > 0 and not rebuild:
        print("Using cached embeddings")
        return collection, True

    if rebuild and existing_count > 0:
        print("Rebuilding index (--rebuild-index flag set)...")
        client.delete_collection(COLLECTION_NAME)
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    # Embed and store
    if chunks is None:
        print("[error] No chunks provided for embedding.", file=sys.stderr)
        sys.exit(1)

    sentence_transformers = _require("sentence-transformers", "sentence_transformers")
    from sentence_transformers import SentenceTransformer

    print(f"Embedding {len(chunks)} chunks with {EMBED_MODEL}...")
    model = SentenceTransformer(EMBED_MODEL)

    texts      = [c["text"]        for c in chunks]
    ids        = [str(c["chunk_index"]) for c in chunks]
    metadatas  = [{"source": c["source"], "page": c["page"], "chunk_index": c["chunk_index"]} for c in chunks]

    # Batch to avoid memory issues
    batch_size = 256
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i:i+batch_size]
        batch_ids   = ids[i:i+batch_size]
        batch_meta  = metadatas[i:i+batch_size]
        embeddings  = model.encode(batch_texts, show_progress_bar=False).tolist()
        collection.add(
            documents=batch_texts,
            embeddings=embeddings,
            ids=batch_ids,
            metadatas=batch_meta,
        )
        print(f"  Indexed {min(i+batch_size, len(texts))}/{len(texts)} chunks", end="\r")
    print()

    return collection, False


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def retrieve(collection, query: str, top_k: int) -> tuple[list[str], float, float]:
    """Embed query, search collection. Returns (chunks, embedding_ms, retrieval_ms)."""
    sentence_transformers = _require("sentence-transformers", "sentence_transformers")
    from sentence_transformers import SentenceTransformer

    t0 = time.perf_counter()
    model = SentenceTransformer(EMBED_MODEL)
    query_embedding = model.encode([query])[0].tolist()
    embedding_ms = (time.perf_counter() - t0) * 1000

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
        source = meta.get("source", "unknown")
        page   = meta.get("page", "?")
        chunk_texts.append(f"[{source} — page {page}]\n{doc}")

    return chunk_texts, embedding_ms, retrieval_ms


# ---------------------------------------------------------------------------
# LLM inference
# ---------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434"


def run_inference(user_message: str, model: str, ollama_url: str = OLLAMA_URL) -> tuple[str, int, int, float]:
    """Call Ollama; return (answer, input_tokens, output_tokens, inference_ms)."""
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "stream": False,
        "options": {
            "num_ctx": 8192,   # override default 4096 — needed for rag_large (8 chunks)
        },
    }).encode()

    req = urllib.request.Request(
        f"{ollama_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        print(f"[error] Could not reach Ollama at {ollama_url}: {e}", file=sys.stderr)
        print("        Is Ollama running? Try: ollama serve", file=sys.stderr)
        sys.exit(1)
    inference_ms = (time.perf_counter() - t0) * 1000

    data = json.loads(raw)

    if "error" in data:
        print(f"[error] Ollama returned an error: {data['error']}", file=sys.stderr)
        sys.exit(1)

    answer        = data["message"]["content"]
    input_tokens  = data.get("prompt_eval_count", 0)
    output_tokens = data.get("eval_count", 0)

    return answer, input_tokens, output_tokens, inference_ms


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def save_results(data: dict, mode: str):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{ts}_{mode}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n[saved] {path}")


def print_summary(data: dict):
    print("\n" + "=" * 60)
    print(f"  Mode            : {data['mode']}")
    print(f"  Model           : {data['model']}")
    print(f"  Chunks retrieved: {data['chunks_retrieved']}")
    print(f"  Embedding ms    : {data['embedding_ms']:.1f}")
    print(f"  Retrieval ms    : {data['retrieval_ms']:.1f}")
    print(f"  Inference ms    : {data['inference_ms']:.1f}")
    print(f"  Input tokens    : {data['input_tokens']}")
    print(f"  Output tokens   : {data['output_tokens']}")
    print(f"  Tokens/sec      : {data['tokens_per_sec']:.1f}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="RAG measurement prototype — baseline / rag / rag_large"
    )
    parser.add_argument(
        "--corpus", required=True,
        help="Path to directory containing PDF files (searched recursively)",
    )
    parser.add_argument(
        "--question", required=True,
        help="Question to answer",
    )
    parser.add_argument(
        "--mode", choices=["baseline", "rag", "rag_large"], default="rag",
        help="Inference mode (default: rag)",
    )
    parser.add_argument(
        "--model", default="mistral",
        help="Ollama model name (default: mistral). Use 'tinyllama' for the lighter model.",
    )
    parser.add_argument(
        "--ollama-url", default=OLLAMA_URL,
        help=f"Ollama base URL (default: {OLLAMA_URL})",
    )
    parser.add_argument(
        "--rebuild-index", action="store_true",
        help="Force re-embedding even if .chroma/ cache exists",
    )
    args = parser.parse_args()

    corpus_path = Path(args.corpus).expanduser().resolve()
    if not corpus_path.is_dir():
        print(f"[error] Corpus path does not exist or is not a directory: {corpus_path}", file=sys.stderr)
        sys.exit(1)

    top_k = TOP_K[args.mode]

    # ------------------------------------------------------------------
    # Build / load index (always needed for rag modes; needed to build
    # cache even for baseline so subsequent rag runs are fast)
    # ------------------------------------------------------------------
    if args.mode == "baseline":
        # Baseline: skip retrieval entirely
        embedding_ms  = 0.0
        retrieval_ms  = 0.0
        chunks_retrieved = 0
        user_message  = BASELINE_PROMPT.format(question=args.question)
    else:
        # Load PDFs and ensure index exists
        pages  = load_pdfs(corpus_path)
        chunks = chunk_pages(pages)
        print(f"Total chunks: {len(chunks)}")

        collection, was_cached = get_chroma_collection(
            chroma_dir=CHROMA_DIR,
            rebuild=args.rebuild_index,
            chunks=chunks,
        )

        chunk_texts, embedding_ms, retrieval_ms = retrieve(collection, args.question, top_k)
        chunks_retrieved = len(chunk_texts)

        joined_chunks = "\n\n".join(chunk_texts)
        user_message  = RETRIEVAL_PROMPT.format(chunks=joined_chunks, question=args.question)

    # ------------------------------------------------------------------
    # LLM inference (fresh client each run — no cached state)
    # ------------------------------------------------------------------
    print(f"\nRunning {args.mode} inference with {args.model} via {args.ollama_url}...")
    answer, input_tokens, output_tokens, inference_ms = run_inference(user_message, args.model, args.ollama_url)

    tokens_per_sec = output_tokens / (inference_ms / 1000) if inference_ms > 0 else 0.0

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    print("\n" + "-" * 60)
    print("ANSWER:")
    print("-" * 60)
    print(answer)

    result = {
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "mode":             args.mode,
        "model":            args.model,
        "corpus_path":      str(corpus_path),
        "question":         args.question,
        "answer":           answer,
        "chunk_size":       512,
        "top_k":            top_k,
        "chunks_retrieved": chunks_retrieved,
        "embedding_ms":     round(embedding_ms, 2),
        "retrieval_ms":     round(retrieval_ms, 2),
        "inference_ms":     round(inference_ms, 2),
        "input_tokens":     input_tokens,
        "output_tokens":    output_tokens,
        "tokens_per_sec":   round(tokens_per_sec, 2),
    }

    save_results(result, args.mode)
    print_summary(result)


if __name__ == "__main__":
    main()
