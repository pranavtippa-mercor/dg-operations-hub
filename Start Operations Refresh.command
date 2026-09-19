#!/bin/zsh
cd -- "$(dirname -- "$0")"
python3 scripts/publish.py --watch --push
