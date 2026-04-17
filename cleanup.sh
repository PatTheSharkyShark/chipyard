#!/bin/bash
# Chipyard空间清理脚本 - 安全版本

cd ~/workspace/chipyard
echo "Current size: $(du -sh . | cut -f1)"
echo ""

echo "[1/6] Cleaning Verilator simulation builds..."
make -C sims/verilator clean 2>/dev/null || echo "  (no verilator build to clean)"

echo "[2/6] Cleaning VCS simulation builds..."
make -C sims/vcs clean 2>/dev/null || echo "  (no vcs build to clean)"

echo "[3/6] Removing generated source files..."
find . -path "*/generated-src/*" -type f -delete 2>/dev/null
find . -name "generated-src" -type d -empty -delete 2>/dev/null
echo "  Done"

echo "[4/6] Removing waveform files (*.vcd, *.fst)..."
find . -name "*.vcd" -type f -delete 2>/dev/null
find . -name "*.fst" -type f -delete 2>/dev/null
echo "  Done"

echo "[5/6] Cleaning Scala/sbt caches..."
rm -rf ~/.sbt/boot/*/lib 2>/dev/null
rm -rf ~/.cache/coursier/v1/https 2>/dev/null
echo "  Done"

echo "[6/6] Cleaning toolchains build directories..."
rm -rf toolchains/riscv-tools/build 2>/dev/null
rm -rf toolchains/esp-tools/build 2>/dev/null
rm -rf toolchains/libgloss/build 2>/dev/null
echo "  Done"

echo ""
echo "Cleanup complete!"
echo "New size: $(du -sh . | cut -f1)"