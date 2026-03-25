# frame_stats_processor_yuv422p10le.py
import numpy as np
import hdr_utilities as hdrut


class FrameStatsToExcelProcessor:
    def __init__(self, excel_name="frame_stats.xlsx", texture_scale=2, edge_thresh=10.0):
        self.excel_name = excel_name
        self.texture_scale = int(texture_scale) if int(texture_scale) >= 1 else 1
        self.edge_thresh = float(edge_thresh)
        self.ctx = None
        self.rows = []

    def setup(self, ctx):
        self.ctx = ctx
        self.rows = []
        inp0 = ctx["inputs"][0]
        if inp0.get("pix_fmt") != "yuv422p10le":
            raise ValueError("Expected pix_fmt=yuv422p10le. Got: %s" % inp0.get("pix_fmt"))

    def process(self, frame_idx, frames):
        raw = frames["in0"]
        inp0 = self.ctx["inputs"][0]
        w = inp0["width"]
        h = inp0["height"]

        # ---- Parse ONLY the Y plane ----
        # Layout is: Y (W*H samples) then U ((W/2)*H) then V ((W/2)*H)
        # Each sample is uint16, but only 10 bits are used.
        count = w * h * 2
        ycc = np.frombuffer(raw, dtype=np.uint16, count=count)  # reads first plane only
        # y10 = (ycc & 1023).astype(np.float32)                     # keep 10-bit (0..1023)
        y10 = hdrut.yuv_frame_to_numpy(ycc,(w,h),10,'limited')
        rgb = hdrut.color_convert(y10,hdrut.YCbCr709toRGB709)
        xyz = hdrut.color_convert(rgb,hdrut.RGB709toXYZ)
        y = xyz[:,:,1]

        Ymin = float(np.min(y))
        Ymax = float(np.max(y))
        Ymean = float(np.mean(y))

        # reshape for texture metrics
        yimg = y.reshape((h, w))

        # Optional downsample for speed (recommended for 4K)
        s = self.texture_scale
        if s > 1:
            ytex = yimg[::s, ::s]
        else:
            ytex = yimg

        # Gradient magnitude stats
        gy, gx = np.gradient(ytex)
        grad = np.sqrt(gx * gx + gy * gy)
        grad_mean = float(np.mean(grad))
        grad_std  = float(np.std(grad))

        # Laplacian variance
        c = ytex
        lap = (-4.0 * c
               + np.roll(c,  1, axis=0) + np.roll(c, -1, axis=0)
               + np.roll(c,  1, axis=1) + np.roll(c, -1, axis=1))
        lap_var = float(np.var(lap))

        # Edge density (fraction of pixels above threshold)
        edge_density = float(np.mean(grad > self.edge_thresh))

        self.rows.append([
            int(frame_idx),
            Ymin, Ymax, Ymean,
            grad_mean, grad_std,
            lap_var,
            edge_density
        ])

        return None  # no video outputs

    def teardown(self):
        from openpyxl import Workbook

        out_path = self.ctx["output_dir"] / self.excel_name

        wb = Workbook()
        ws = wb.active
        ws.title = "per_frame"

        ws.append([
            "frame_idx",
            "Y_min", "Y_max", "Y_mean",
            "grad_mean", "grad_std",
            "lap_var",
            "edge_density"
        ])

        for r in self.rows:
            ws.append(r)

        wb.save(out_path)
        print("Wrote:", out_path)