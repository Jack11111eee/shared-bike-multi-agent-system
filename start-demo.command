#!/bin/zsh
# Run from this checkout; no credentials are printed or passed as arguments.
cd "${0:A:h}"
task_python=""
for candidate in "$PWD/.venv/bin/python" "$HOME/miniconda3/bin/python" "$(command -v python3)"; do
  if [[ -x "$candidate" ]] && "$candidate" -c 'import pandas,jsonschema,pyarrow' >/dev/null 2>&1; then
    task_python="$candidate"
    break
  fi
done
if [[ -z "$task_python" ]]; then
  echo "未找到项目 Python 环境。请安装依赖：python3 -m pip install -e '.[llm]'"
  read "?按回车退出"
  exit 1
fi
demo_port=$("$task_python" - <<'PY'
import socket
for port in [*range(8000, 8011), 0]:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            print(sock.getsockname()[1])
            break
        except OSError:
            continue
PY
)
echo "工作台地址：http://127.0.0.1:$demo_port/console/"
echo "本窗口保持运行；按 Ctrl-C 停止服务。"
"$task_python" -m bike_agent.cli serve --port "$demo_port"
