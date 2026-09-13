# 원생노트 · academy_check

관리자 선생님 1명을 위한 온라인 원생 관리 웹앱입니다. Python 3.12 이상만 있으면 실행할 수 있습니다. 외부 Python/JavaScript 패키지 설치는 필요하지 않습니다.

## 실행

프로젝트 루트에서 관리자 비밀번호를 먼저 설정합니다. 비밀번호는 터미널에서 숨김 입력하며 12자 이상이어야 합니다.

```bash
python3 -m academy.server set-password
python3 -m academy.server
```

브라우저에서 http://127.0.0.1:8000 에 접속합니다. 초기 원생 데이터는 비어 있으며 시험 카테고리 5개는 자동 생성됩니다. 종료는 Ctrl+C입니다. 관리자 계정은 하나이며 로그인 화면에는 비밀번호만 입력합니다.

비밀번호를 잊었다면 서버에서 `set-password`를 다시 실행하세요. 기존 로그인 세션은 모두 종료됩니다.

비밀번호 설정 명령은 프로젝트 루트의 `.env`에 `ADMIN_PASSWORD_HASH`를 자동 저장합니다. 평문 비밀번호는 저장하지 않습니다. `.env`는 Git에서 제외되며 소유자만 읽고 쓸 수 있는 권한(600)으로 생성됩니다. 생성된 해시를 직접 수정하거나 `.env`를 셸에서 `source`할 필요가 없습니다. 처음 제공되는 `.env`는 빈 설정이므로 위 설정 명령을 실행해야 합니다.

해시는 scrypt(N=16384, r=8, p=5)와 무작위 salt를 사용합니다. 서버는 시작 시 `.env`를 읽어 DB의 인증 해시를 동기화하고, 값이 변경되었다면 기존 세션을 종료합니다. `.env`가 없거나 잘못되면 시작을 중단합니다. 기존 버전의 DB 사용자도 `set-password`를 한 번 실행해 `.env`를 생성해야 합니다.

다른 설정 파일은 `--env-file /보호된/경로/.env`로 지정하세요. 비밀번호 설정과 서버 실행에 같은 경로를 사용합니다. 파일을 수동 교체했다면 서버를 재시작하세요. `.env`도 인증정보이므로 공개하거나 공유하지 마세요.

## 기능

- 원생 등록·검색·수정·삭제, 동명이인 구분
- 교재명과 사용 중 / 사용 완료 상태 수정
- 날짜별 일지·상담 작성·수정·개별 삭제
- 시험 카테고리 등록·이름 수정
- 100점 만점 성적, 선택 난이도, 카테고리별 날짜순 그래프
- 원생·성적 삭제 확인, 오래된 수정·삭제 요청의 충돌 안내
- 서버 SQLite 영구 저장, 관리자 인증, 매일 자동 백업

화면 재조회 시 서버의 최신 데이터를 읽습니다. 브라우저 영구 저장소나 오프라인 캐시는 사용하지 않습니다. 네트워크 실패 시 폼 입력은 현재 화면의 메모리에 남습니다. 충돌 시 입력을 복사해 두고 취소 → 새로고침 → 수정으로 최신 기록을 확인하세요.

## 데이터와 백업

기본 DB는 `var/academy.sqlite3`, 백업은 `var/backups/`입니다. 서버 시작 시와 실행 중 24시간마다 백업하며 30일이 지난 백업은 다음 백업 시 정리합니다. 백업에는 학생 기록과 관리자 비밀번호 해시가 포함되며 로그인 세션은 제외됩니다. `var/`는 Git에서 제외합니다.

```bash
python3 -m academy.server backup
python3 -m academy.server --db /absolute/path/academy.sqlite3 --backups /absolute/path/backups
```

별도 테스트 환경에 복구하려면 백업을 새 경로로 복사한 뒤 실행하세요. 백업 파일 자체를 운영 DB로 열지 마세요.

```bash
mkdir -p var/restore-test
cp var/backups/선택한백업.sqlite3 var/restore-test/academy.sqlite3
python3 -m academy.server --db var/restore-test/academy.sqlite3 --backups var/restore-test/backups --port 8001
```

실서비스 복구는 서버를 종료한 뒤 별도 디렉터리에 복사한 복구 DB 경로로 재시작합니다. 기존 DB와 WAL 파일을 덮어쓰지 않습니다. 복구 후 계정 접근과 원생별 기록 연결을 확인하세요.

## 검증

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
```

HTTP 통합 테스트는 로컬 소켓 권한이 필요합니다. 인증·CSRF·입력 검증·중복 이름·버전 충돌·삭제·50명 데이터·백업 무결성과 연결 관계를 검사합니다. Node는 JavaScript 구문 검사에만 필요합니다.

배포 설계와 남은 인수 검증은 [Doc/DESIGN.md](Doc/DESIGN.md)를 참고하세요. 실제 학생 데이터를 넣기 전에 HTTPS와 서버 외부 백업 구성을 완료해야 합니다.
