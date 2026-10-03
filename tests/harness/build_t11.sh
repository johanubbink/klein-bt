#!/bin/sh
# Build a copy of BehaviorTree.CPP's examples/t11_groot_howto.cpp that takes its
# Groot2 port as argv[1] (upstream hard-codes 1667), against an existing build
# of the library. The checkout is only read; everything lands in OUT_DIR.
#
#   build_t11.sh BT_DIR OUT_DIR      -> OUT_DIR/t11_port
set -eu
BT=$(cd "$1" && pwd)
mkdir -p "$2"
OUT=$(cd "$2" && pwd)

sed -e 's/^int main()/int main(int argc, char** argv)/' \
    -e 's/const unsigned port = 1667;/const unsigned port = argc > 1 ? std::atoi(argv[1]) : 1667;/' \
    "$BT/examples/t11_groot_howto.cpp" > "$OUT/t11_port.cpp"
# Refuse to build a binary that would silently ignore the port argument.
grep -q 'argc > 1 ? std::atoi(argv\[1\])' "$OUT/t11_port.cpp" || {
    echo "build_t11.sh: port patch did not apply; t11_groot_howto.cpp changed upstream" >&2
    exit 2
}

cd "$BT"
g++ -std=c++17 -O1 -I include -I sample_nodes -I 3rdparty "$OUT/t11_port.cpp" \
    -o "$OUT/t11_port.tmp" build/sample_nodes/lib/libbt_sample_nodes.a \
    -L build -lbehaviortree_cpp -Wl,-rpath,"$BT/build" -lpthread
mv "$OUT/t11_port.tmp" "$OUT/t11_port"
