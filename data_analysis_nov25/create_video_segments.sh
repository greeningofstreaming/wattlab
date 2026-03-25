# 1) Trim Elfuente to 180 seconds at 1080p30
ffmpeg -i Elfuente_crf15_2160p5994_420_8bit.mp4 \
       -t 180 \
       -vf "scale=1920:1080,fps=30" \
       -c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le \
       -c:a copy \
       elfuente_180s_1080p30_prores422b.mov

ffmpeg -f lavfi -video_size 1920x1080 -framerate 30 \
       -i "color=c=black" \
       -t 30 \
       -c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le \
       -an \
       black30s_1080p30_prores422.mov

ffmpeg -f lavfi -video_size 1920x1080 -framerate 30 \
       -i "color=c=black" \
       -t 30 \
       -c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le \
       -an \
       black30s_1080p30_prores422.mov

ffmpeg -f lavfi -video_size 1920x1080 -framerate 30 \
       -i "noise=alls=20:allf=t+u" \
       -t 30 \
       -c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le \
       -an \
       noise30s_1080p30_prores422.mov