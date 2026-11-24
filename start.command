#!/bin/bash
# 더블클릭하면 서버가 시작되고 브라우저가 열립니다. 종료는 이 창을 닫으세요.
cd "$(dirname "$0")" && python3 -m academy.server --local
