#!/usr/bin/env sh
set -eu

output_path="${1:-/tmp/crime-prediction-demo.mp4}"
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "color=c=0x171113:s=1280x720:d=8" \
  -vf "drawbox=x=80+70*t:y=290:w=110:h=170:color=0xf40c3f@0.75:t=fill,drawtext=text='SYNTHETIC SAFETY DEMO':fontcolor=white:fontsize=36:x=50:y=50" \
  -an -c:v libx264 -pix_fmt yuv420p -movflags +faststart "$output_path"
printf '%s\n' "$output_path"
