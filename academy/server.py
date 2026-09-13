"""단일 관리자용 서버. 데이터와 세션은 서버 SQLite에만 보관한다."""
import argparse
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import tempfile
import re
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = ['중학교 내신 영어', '고등학교 내신 영어', '수능 영어', '모의고사 영어', '영어듣기평가']
SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS admin(id INTEGER PRIMARY KEY CHECK(id=1), salt TEXT NOT NULL, password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, school TEXT NOT NULL DEFAULT '', grade TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS books(id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE, title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('사용 중','사용 완료')), version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE, date TEXT NOT NULL, content TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS scores(id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE, category_id INTEGER NOT NULL REFERENCES categories(id), date TEXT NOT NULL, title TEXT NOT NULL, score REAL NOT NULL CHECK(score BETWEEN 0 AND 100), difficulty TEXT NOT NULL CHECK(difficulty IN ('','쉬움','보통','어려움')), version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS audit_times(kind TEXT NOT NULL, record_id INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(kind,record_id));
'''
FIELDS = {'students': ['name','school','grade'], 'categories':['name'], 'books':['student_id','title','status'], 'notes':['student_id','date','content'], 'scores':['student_id','category_id','date','title','score','difficulty']}


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript(SCHEMA)
        # 기존 총점 기록은 그대로 두고 영역별 점수 열만 추가한다.
        columns = {row['name'] for row in db.execute('PRAGMA table_info(scores)')}
        for column in ('objective_score', 'written_score', 'objective_max', 'written_max'):
            if column not in columns:
                db.execute(f'ALTER TABLE scores ADD COLUMN {column} REAL')
        if 'score_mode' not in columns:
            db.execute("ALTER TABLE scores ADD COLUMN score_mode TEXT NOT NULL DEFAULT 'total'")
        if not db.execute('SELECT 1 FROM categories').fetchone():
            db.executemany('INSERT INTO categories(name) VALUES(?)', [(n,) for n in CATEGORIES])
    os.chmod(path, 0o600)


def sync_credentials(path, salt, digest):
    with connect(path) as db:
        old = db.execute('SELECT salt,password FROM admin WHERE id=1').fetchone()
        if old and old['salt'] == salt and old['password'] == digest:
            return
        db.execute('INSERT OR REPLACE INTO admin VALUES(1,?,?)', (salt, digest))
        db.execute('DELETE FROM sessions')


def load_credentials(path, env_path):
    # 우선순위:
    # 1. OS 환경변수 ADMIN_PASSWORD_HASH
    # 2. 로컬 .env 파일
    #
    # 설정이 존재하지만 잘못된 경우 다른 설정으로 우회하지 않는다.

    pattern = r'scrypt:16384:8:5:[0-9a-f]{64}:[0-9a-f]{128}'

    # 1. 운영환경(Cloudtype 등)의 OS 환경변수 확인
    env_value = os.environ.get('ADMIN_PASSWORD_HASH')

    if env_value is not None:
        env_value = env_value.strip()

        if not re.fullmatch(pattern, env_value):
            raise ValueError(
                '환경변수 ADMIN_PASSWORD_HASH 설정이 올바르지 않습니다.'
            )

        credential = env_value

    # 2. 환경변수가 없을 때만 로컬 .env 사용
    else:
        env_path = Path(env_path)

        if env_path.is_symlink() or not env_path.is_file():
            raise ValueError(
                'ADMIN_PASSWORD_HASH 환경변수 또는 .env 설정이 없습니다. '
                '로컬 환경에서는 set-password를 실행하세요.'
            )

        if env_path.stat().st_mode & 0o077:
            raise ValueError(
                '.env 권한을 600으로 설정하세요: chmod 600 ' + str(env_path)
            )

        entries = [
            line.split('=', 1)[1].strip()
            for line in env_path.read_text().splitlines()
            if '=' in line
            and line.split('=', 1)[0].strip() == 'ADMIN_PASSWORD_HASH'
        ]

        if len(entries) != 1:
            raise ValueError(
                'ADMIN_PASSWORD_HASH 설정이 없거나 중복되어 있습니다. '
                'set-password를 실행하세요.'
            )

        if not re.fullmatch(pattern, entries[0]):
            raise ValueError(
                'ADMIN_PASSWORD_HASH 설정이 올바르지 않습니다. '
                'set-password를 실행하세요.'
            )

        credential = entries[0]

    # DB 동기화
    salt, digest = credential.split(':')[-2:]
    sync_credentials(path, salt, 'scrypt5:' + digest)


def set_password(path, password, env_path=None):
    if len(password) < 12:
        raise ValueError('비밀번호는 12자 이상이어야 합니다.')
    salt = secrets.token_hex(32)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=5).hex()
    if env_path is not None:
        env_path = Path(env_path)
        if env_path.is_symlink():
            raise ValueError('.env 심볼릭 링크에는 저장할 수 없습니다.')
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        lines = [line for line in lines if line.split('=', 1)[0].strip() not in
                 ('ADMIN_PASSWORD_HASH', 'ADMIN_PASSWORD')]
        lines.append(f'ADMIN_PASSWORD_HASH=scrypt:16384:8:5:{salt}:{digest}')
        # 임시 파일도 소유자만 읽을 수 있게 만든 뒤 원자적으로 교체한다.
        fd, temporary = tempfile.mkstemp(dir=env_path.parent, prefix='.credential-')
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write('\n'.join(lines) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, env_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    sync_credentials(path, salt, 'scrypt5:' + digest)


def backup(path, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.sqlite3')
    with connect(path) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
        dest.execute('DELETE FROM sessions')
    os.chmod(target, 0o600)
    for old in directory.glob('*.sqlite3'):
        if old.stat().st_mtime < time.time() - 30 * 86400:
            old.unlink()
    return target


class Invalid(Exception):
    def __init__(self, message, status=400, field=None):
        self.message, self.status, self.field = message, status, field


def validate(kind, data):
    parts = ('objective_score', 'written_score', 'objective_max', 'written_max')
    allowed = set(FIELDS[kind]) | {'version'}
    if kind == 'scores':
        allowed |= set(parts) | {'score_mode'}
    if set(data) - allowed:
        raise Invalid('허용하지 않는 입력 항목입니다. 만점은 100점으로 고정됩니다.')
    result = {}
    if kind == 'scores':
        data = dict(data)
        mode = data.get('score_mode', 'total')
        if mode not in ('total', 'split'):
            raise Invalid('점수 입력 방식을 선택해 주세요.', field='score_mode')
        result['score_mode'] = mode
        if mode == 'split':
            numbers = {}
            for field in parts:
                value = data.get(field)
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                    raise Invalid('0~100 사이의 숫자를 입력해 주세요.', field=field)
                numbers[field] = Decimal(str(value))
            if numbers['objective_max'] + numbers['written_max'] != 100:
                raise Invalid('객관식과 서술형 만점의 합은 100점이어야 합니다.', field='written_max')
            for prefix in ('objective', 'written'):
                if numbers[prefix + '_score'] > numbers[prefix + '_max']:
                    raise Invalid('해당 영역의 만점을 초과할 수 없습니다.', field=prefix + '_score')
            # 총점은 클라이언트가 보낸 값을 신뢰하지 않고 영역별 점수로 계산한다.
            data['score'] = float(numbers['objective_score'] + numbers['written_score'])
            result.update({field: float(value) for field, value in numbers.items()})
        else:
            if any(data.get(field) is not None for field in parts):
                raise Invalid('영역별 점수는 나눠 입력 방식을 선택해 주세요.', field='score_mode')
            result.update({field: None for field in parts})
    for field in FIELDS[kind]:
        value = data.get(field, '')
        if field in ('student_id', 'category_id'):
            if type(value) is not int or value < 1:
                raise Invalid('항목을 선택해 주세요.', field=field)
        elif field == 'score':
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                raise Invalid('점수는 0~100 사이의 숫자여야 합니다.', field=field)
        else:
            if not isinstance(value, str):
                raise Invalid('올바른 문자를 입력해 주세요.', field=field)
            value = value.strip()
            if field not in ('school','grade','difficulty') and not value:
                raise Invalid('필수 항목입니다.', field=field)
            if len(value) > (20000 if field == 'content' else 200):
                raise Invalid('입력 내용이 너무 깁니다.', field=field)
        if field == 'date':
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise Invalid('날짜를 YYYY-MM-DD 형식으로 입력해 주세요.', field=field)
        if field == 'status' and value not in ('사용 중','사용 완료'):
            raise Invalid('교재 상태를 선택해 주세요.', field=field)
        if field == 'difficulty' and value not in ('','쉬움','보통','어려움'):
            raise Invalid('올바른 난이도를 선택해 주세요.', field=field)
        result[field] = value
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 개인정보·요청 본문·인증값을 로그에 남기지 않는다.

    def reply(self, status, payload, cookie=None, content_type='application/json; charset=utf-8'):
        body = json.dumps(payload, ensure_ascii=False).encode() if content_type.startswith('application/json') else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError()
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeError):
            raise Invalid('올바른 요청 본문이 필요합니다.')

    def session(self, db):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            raise Invalid('로그인이 필요합니다.', 401)
        token = cookies.get('session')
        row = db.execute('SELECT * FROM sessions WHERE token=? AND expires>?', (hashlib.sha256(token.value.encode()).hexdigest() if token else '', time.time())).fetchone()
        if not row:
            raise Invalid('로그인이 필요합니다.', 401)
        if self.command != 'GET' and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''), row['csrf']):
            raise Invalid('세션을 다시 확인해 주세요.', 403)
        return row

    def cookie(self, token, age):
        return f'session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}' + ('; Secure' if self.server.secure else '')

    def handle_request(self):
        try:
            path = self.path.split('?')[0]
            if not path.startswith('/api/'):
                files = {'/vendor/bootstrap.min.css': ('vendor/bootstrap.min.css','text/css; charset=utf-8'), '/': ('index.html','text/html; charset=utf-8'), '/app.js': ('app.js','text/javascript; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8')}
                if self.command != 'GET' or path not in files:
                    raise Invalid('찾을 수 없습니다.',404)
                filename, mime = files[path]
                return self.reply(200, (ROOT / 'static' / filename).read_bytes(), content_type=mime)
            with connect(self.server.db_path) as db:
                if path == '/api/login' and self.command == 'POST':
                    data = self.body()
                    with self.server.login_lock:
                        now = time.time()
                        self.server.attempts = [t for t in self.server.attempts if t > now-300]
                        if len(self.server.attempts) >= 10:
                            raise Invalid('로그인 시도가 많습니다. 5분 후 다시 시도해 주세요.',429)
                        self.server.attempts.append(now)
                    admin = db.execute('SELECT * FROM admin WHERE id=1').fetchone()
                    password = data.get('password','')
                    if not isinstance(password,str) or not admin:
                        raise Invalid('로그인 정보를 확인해 주세요.',401)
                    stored = admin['password']
                    work = 5 if stored.startswith('scrypt5:') else 1
                    digest = hashlib.scrypt(password.encode(), salt=admin['salt'].encode(), n=16384,r=8,p=work).hex()
                    if not hmac.compare_digest(digest,stored.removeprefix('scrypt5:')):
                        raise Invalid('로그인 정보를 확인해 주세요.',401)
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    db.execute('DELETE FROM sessions WHERE expires<=?',(time.time(),))
                    db.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,time.time()+43200))
                    db.commit()
                    return self.reply(200, {'csrf':csrf}, self.cookie(token,43200))
                session = self.session(db)
                if path == '/api/session' and self.command == 'GET':
                    return self.reply(200, {'csrf':session['csrf']})
                if path == '/api/logout' and self.command == 'POST':
                    db.execute('DELETE FROM sessions WHERE token=?',(session['token'],))
                    db.commit()
                    return self.reply(200, {},self.cookie('',0))
                parts = path.strip('/').split('/')
                kind = parts[1]
                if kind not in FIELDS or len(parts) > 3:
                    raise Invalid('찾을 수 없습니다.',404)
                record_id = None
                if len(parts) == 3:
                    try:
                        record_id = int(parts[2])
                    except ValueError:
                        raise Invalid('찾을 수 없습니다.',404)
                if self.command == 'GET':
                    rows = db.execute(f'SELECT * FROM {kind} ORDER BY id').fetchall()
                    return self.reply(200,[dict(r) for r in rows])
                data = self.body()
                # 쓰기 잠금 안에서 버전을 검사하고 변경해 동시 요청의 덮어쓰기를 막는다.
                db.execute('BEGIN IMMEDIATE')
                if self.command in ('PUT','DELETE'):
                    row = db.execute(f'SELECT * FROM {kind} WHERE id=?',(record_id,)).fetchone()
                    if not row:
                        raise Invalid('이미 삭제된 기록입니다. 새로고침해 주세요.',404)
                    if type(data.get('version')) is not int or data['version'] != row['version']:
                        raise Invalid('다른 기기에서 변경되었습니다. 입력 내용을 확인한 뒤 새로고침해 주세요.',409)
                if self.command == 'DELETE':
                    if kind not in ('students','notes','scores'):
                        raise Invalid('지원하지 않는 삭제입니다.',405)
                    if kind == 'students':
                        for child in ('books','notes','scores'):
                            db.execute(f'DELETE FROM audit_times WHERE kind=? AND record_id IN (SELECT id FROM {child} WHERE student_id=?)',(child,record_id))
                    db.execute(f'DELETE FROM {kind} WHERE id=?',(record_id,))
                    db.execute('DELETE FROM audit_times WHERE kind=? AND record_id=?',(kind,record_id))
                    db.commit()
                    return self.reply(200,{'ok':True})
                if self.command not in ('POST','PUT') or (self.command == 'POST' and record_id is not None):
                    raise Invalid('지원하지 않는 요청입니다.',405)
                values = validate(kind,data)
                if self.command == 'PUT' and 'student_id' in values and values['student_id'] != row['student_id']:
                    raise Invalid('기록의 원생을 변경할 수 없습니다.')
                if self.command == 'POST':
                    columns = ','.join(values)
                    record_id = db.execute(f'INSERT INTO {kind}({columns}) VALUES({",".join("?" for _ in values)})',tuple(values.values())).lastrowid
                else:
                    db.execute(f'UPDATE {kind} SET {",".join(k+"=?" for k in values)},version=version+1 WHERE id=?',(*values.values(),record_id))
                now = datetime.now(timezone.utc).isoformat()
                db.execute('INSERT INTO audit_times VALUES(?,?,?,?) ON CONFLICT(kind,record_id) DO UPDATE SET updated_at=excluded.updated_at',(kind,record_id,now,now))
                result = dict(db.execute(f'SELECT * FROM {kind} WHERE id=?',(record_id,)).fetchone())
                db.commit()
                return self.reply(200,result)
        except Invalid as exc:
            self.reply(exc.status,{'error':exc.message,'field':exc.field})
        except sqlite3.IntegrityError:
            self.reply(400,{'error':'중복된 카테고리 이름이거나 연결 대상이 삭제되었습니다.','field':'name'})
        except Exception:
            self.reply(500,{'error':'서버 요청에 실패했습니다. 입력을 유지하고 다시 시도해 주세요.'})

    do_GET = do_POST = do_PUT = do_DELETE = handle_request


def make_server(path, host='127.0.0.1', port=8000, secure=False):
    initialize(path)
    server = ThreadingHTTPServer((host,port),Handler)
    server.db_path, server.secure = path, secure
    server.login_lock, server.attempts = threading.Lock(), []
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['serve','set-password','backup'], nargs='?', default='serve')
    parser.add_argument('--db',default='var/academy.sqlite3')
    parser.add_argument('--env-file', default=str(ROOT / '.env'))
    parser.add_argument('--backups',default='var/backups')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--secure',action='store_true',help='HTTPS 프록시 뒤에서 Secure 쿠키 사용')
    args = parser.parse_args()
    os.umask(0o077)
    initialize(args.db)
    if args.action == 'set-password':
        import getpass
        password = getpass.getpass('새 관리자 비밀번호 (12자 이상): ')
        if password != getpass.getpass('다시 입력: '):
            raise SystemExit('비밀번호가 일치하지 않습니다.')
        set_password(args.db,password,args.env_file)
        print('관리자 비밀번호를 설정했습니다. 기존 세션을 종료했습니다.')
    elif args.action == 'backup':
        print(backup(args.db,args.backups))
    else:
        try:
            load_credentials(args.db, args.env_file)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        backup(args.db,args.backups)
        def schedule():
            while True:
                time.sleep(86400)
                try:
                    backup(args.db,args.backups)
                except Exception:
                    print('자동 백업 실패: 저장 공간과 백업 경로 권한을 확인하세요.',flush=True)
        threading.Thread(target=schedule,daemon=True).start()
        server = make_server(args.db,args.host,args.port,args.secure)
        print(f'http://{args.host}:{args.port} 에서 실행 중',flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
