#!/usr/bin/env bash
# 拉取 FP32 权重，用于「量化前后」体积与质量对比。
# INT8 权重已随仓库提供；FP32 约 86 MiB，不入库。
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)/models/all-MiniLM-L6-v2/onnx"
mkdir -p "$DIR"
BASE1="https://hf-mirror.com/Xenova/all-MiniLM-L6-v2/resolve/main"
BASE2="https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main"

echo "下载 FP32 权重到 $DIR/model.onnx"
if curl -fL --retry 3 -o "$DIR/model.onnx" "$BASE1/onnx/model.onnx"; then
  echo "完成（mirror）"
else
  echo "镜像失败，尝试官方源"
  curl -fL --retry 3 -o "$DIR/model.onnx" "$BASE2/onnx/model.onnx"
  echo "完成（hf）"
fi

ls -l "$DIR"
node -e '
const fs=require("fs");
const d=process.argv[1];
const q=fs.statSync(d+"/model_quantized.onnx").size;
const f=fs.statSync(d+"/model.onnx").size;
console.log(`FP32 ${f} B  INT8 ${q} B  压缩比 ${(f/q).toFixed(2)}x`);
' "$DIR"
