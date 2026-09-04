#!/bin/bash
# TPC-DS dsdgen 工具链 bootstrap + SF0.1 源数据生成（Atlas 零售域装载前序，P2）
#
# 职责：
#   1) clone gregrahn/tpcds-kit（固定版本 5a3a817，v2.10.0）到 data/raw/tpcds-kit/
#   2) macOS 本地编译 dsdgen/distcomp（Makefile.suite OS=MACOS）
#   3) 应用 scripts/tpcds_kit_sf01.patch（SF0.1 社区扩展档，见下）
#   4) distcomp -input tpcds.dst -output dists.dmp（dists.dmp = 67 个分布注册，
#      tpcds.dst 自带 8 条 #include 指令，distcomp 原生支持嵌套解析，勿手工拼接）
#   5) dsdgen 生成 SF0.1 四表源数据 data/raw/tpcds/sf01/*.dat
#      （date_dim/item/store/store_sales；store_returns 为 store_sales 连带
#       FL_PARENT child 产物，生成后删除——装载不覆盖该表）
#
# SF0.1 口径声明（如实，勿改）：dsdgen 官方 SCALE 为整型（SF1..SF100000），
#   补丁使其支持 0<scale<1：事实表（FL_DATE_BASED）按 SF1 行数 ×scale 线性外推，
#   维表保持 SF1 基数（保品类/州值域完整）。本机实测（2026-09-04，输出确定性
#   已验证——两次运行逐字节一致）：
#     date_dim 73,049 / item 18,000 / store 12 / store_sales 240,485 行
#     （≈ SF1 2,880,404 的 1/12，dsdgen 离散化所致，勿外推为精确 1/10）
#
# 前置：macOS 需要 brew bison >= 3（语法生成；仓库已预生成 grammar.c，
#   fresh clone 时间戳一致不会重跑 yacc，仍建议装齐避免 .y 变更场景）
#   编译环境实测参数：make OS=MACOS MACOS_CFLAGS="-g -Wall -Wno-implicit-int
#   -Wno-deprecated-non-prototype -Wno-return-type"
#
# 降级路径（网络/编译失败不阻塞数据装载）：
#   - 手工预置 clone：git clone https://github.com/gregrahn/tpcds-kit.git 到
#     data/raw/tpcds-kit/ 并 checkout 5a3a817 后重跑（检测到 tools/dsdgen 与
#     tools/dists.dmp 即跳过编译/补丁步骤）
#   - 或任选含 dsdgen 的发行形态手工生成 .dat 放 data/raw/tpcds/sf01/，
#     data/tpcds_loader.py 独立装载（本脚本非其必要条件）
#
# 用法：scripts/setup_tpcds.sh（从仓库根执行；幂等——工具链与数据产物齐全即跳过）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KIT_SHA=5a3a81796992b725c2a8b216767e142609966752   # gregrahn/tpcds-kit v2.10.0
KIT_DIR="${TPC_DS_KIT_DIR:-$ROOT/data/raw/tpcds-kit}"
OUT_DIR="$ROOT/data/raw/tpcds/sf01"
PATCH="$ROOT/scripts/tpcds_kit_sf01.patch"
SCALE="${TPC_DS_SCALE:-0.1}"
TABLES=(date_dim item store store_sales)

echo "==> Atlas TPC-DS 工具链 bootstrap（scale=${SCALE}）"
echo "    kit: $KIT_DIR"

# ---------------------------------------------------------------------------
# 1) clone / 复用固定版本
# ---------------------------------------------------------------------------
if [[ ! -d "$KIT_DIR/.git" ]]; then
    echo "==> clone gregrahn/tpcds-kit @ $KIT_SHA"
    mkdir -p "$(dirname "$KIT_DIR")"
    git clone https://github.com/gregrahn/tpcds-kit.git "$KIT_DIR"
    git -C "$KIT_DIR" checkout "$KIT_SHA"
else
    echo "==> 复用已有 clone: $KIT_DIR"
fi

# 校验版本：产物齐全则跳过编译（手工预置路径也走这里）
if [[ -x "$KIT_DIR/tools/dsdgen" && -x "$KIT_DIR/tools/distcomp" && -f "$KIT_DIR/tools/dists.dmp" ]]; then
    echo "==> 工具链产物齐全（dsdgen/distcomp/dists.dmp），跳过编译与补丁步骤"
else
    # -----------------------------------------------------------------------
    # 2) macOS 编译环境检查（bison >= 3，PATH 前缀）
    # -----------------------------------------------------------------------
    if [[ "$(uname)" == "Darwin" ]]; then
        for cand in /opt/homebrew/opt/bison/bin /usr/local/opt/bison/bin; do
            if [[ -x "$cand/bison" ]]; then
                export PATH="$cand:$PATH"
                break
            fi
        done
        if ! command -v bison >/dev/null 2>&1; then
            echo "!! 未找到 bison：请先 brew install bison（tpcds-kit 语法生成需要 >= 3）" >&2
            echo "   降级：按脚本头注释手工预置工具链后重跑" >&2
            exit 1
        fi
        bison_ver="$(bison --version | head -1)"
        echo "==> bison: $bison_ver"
    fi

    # -----------------------------------------------------------------------
    # 3) 应用 SF0.1 补丁（幂等：已应用则跳过）
    # -----------------------------------------------------------------------
    if git -C "$KIT_DIR" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
        echo "==> SF0.1 补丁已应用，跳过"
    else
        echo "==> apply scripts/tpcds_kit_sf01.patch"
        git -C "$KIT_DIR" apply "$PATCH"
    fi

    # -----------------------------------------------------------------------
    # 4) 编译 dsdgen/distcomp（产物与实验验证参数一致）
    # -----------------------------------------------------------------------
    echo "==> make dsdgen distcomp（OS=MACOS）"
    make -C "$KIT_DIR/tools" OS=MACOS \
        MACOS_CFLAGS="-g -Wall -Wno-implicit-int -Wno-deprecated-non-prototype -Wno-return-type" \
        dsdgen distcomp

    # dists.dmp：官方 67 分布注册产物（distcomp 在 tools/ 目录内执行）
    echo "==> distcomp -> tools/dists.dmp"
    (cd "$KIT_DIR/tools" && ./distcomp -input tpcds.dst -output dists.dmp)
fi

# 产物校验
"$KIT_DIR/tools/dsdgen" -h >/dev/null 2>&1 || { echo "!! dsdgen 不可执行" >&2; exit 1; }
[[ -s "$KIT_DIR/tools/dists.dmp" ]] || { echo "!! dists.dmp 缺失或为空" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 5) 生成 SF0.1 四表源数据（确定性输出，可复现；重复运行先清旧文件防混）
# ---------------------------------------------------------------------------
if [[ -f "$OUT_DIR/date_dim.dat" && -f "$OUT_DIR/item.dat" && \
      -f "$OUT_DIR/store.dat" && -f "$OUT_DIR/store_sales.dat" ]]; then
    echo "==> 源数据已存在（${OUT_DIR}），跳过生成（删除该目录可强制重生成）"
else
    echo "==> dsdgen -scale $SCALE -> $OUT_DIR"
    rm -rf "$OUT_DIR"
    mkdir -p "$OUT_DIR"
    for t in "${TABLES[@]}"; do
        (cd "$KIT_DIR/tools" && ./dsdgen -scale "$SCALE" -dir "$OUT_DIR" -table "$t" >/dev/null 2>&1)
        echo "    done: $t"
    done
    # store_sales 连带产物（FL_PARENT child），不装载
    rm -f "$OUT_DIR/store_returns.dat"
fi

echo "==> 产物校验（行数）"
for t in "${TABLES[@]}"; do
    rows="$(wc -l < "$OUT_DIR/$t.dat" | tr -d ' ')"
    echo "    $t.dat: $rows 行"
done
echo "==> setup_tpcds 完成（下一步：uv run python data/tpcds_loader.py 或 make seed-retail）"
