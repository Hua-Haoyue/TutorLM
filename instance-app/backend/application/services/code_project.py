"""Virtual source files and a revision-aware static index; never run imported code."""
import ast
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

from backend.domain.errors import ConflictError, InvalidInputError

EXTENSIONS = {'.py': 'python', '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript', '.jsx': 'javascript', '.java': 'java', '.c': 'c',
    '.h': 'c', '.cpp': 'cpp', '.hpp': 'cpp', '.go': 'go', '.rs': 'rust', '.sh': 'bash',
    '.sql': 'sql', '.css': 'css', '.html': 'xml', '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
    '.toml': 'ini', '.rb': 'ruby', '.php': 'php', '.swift': 'swift', '.kt': 'kotlin', '.vue': 'xml'}
PROJECT_EXTENSIONS = EXTENSIONS | {'.md': 'markdown', '.txt': 'plaintext'}
BLOCKED = {'.git', '.venv', 'node_modules', '__pycache__', 'dist', 'credentials', 'secrets'}


def source_path(value):
    if not isinstance(value, str) or not value or len(value) > 240 or '\\' in value or '\x00' in value or ':' in value:
        raise InvalidInputError('Invalid project-relative source path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') or p.startswith('.') or p in BLOCKED for p in value.split('/')):
        raise InvalidInputError('Hidden, credential, traversal and dependency paths are not allowed')
    if path.suffix.lower() not in PROJECT_EXTENSIONS or re.search(r'(secret|credential|private.?key|id_rsa)', path.name, re.I):
        raise InvalidInputError('Unsupported or sensitive source file')
    return str(path)


def symbols(text, language):
    if language == 'python':
        try:
            tree = ast.parse(text)
            return [{'name': n.name, 'line': n.lineno, 'kind': type(n).__name__} for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))][:500]
        except (SyntaxError, ValueError, RecursionError):
            return []
    return [{'name': m.group(1), 'line': text.count('\n', 0, m.start()) + 1, 'kind': 'definition'}
            for m in list(re.finditer(r'\b(?:function|class|interface|def|fn|func|struct)\s+(\w+)', text))[:500]]


class CodeProject:
    def __init__(self, app):
        self.app = app
        with app.store.transaction() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS code_index(document_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, path TEXT NOT NULL UNIQUE, language TEXT NOT NULL, text TEXT NOT NULL, symbols_json TEXT NOT NULL)')

    def refresh(self):
        docs = self.app.store.list_documents(self.app.current_project()['id'])
        live = set()
        with self.app.store.transaction() as conn:
            for doc in docs:
                extra = json.loads(doc.get('extra_json') or '{}')
                path = extra.get('code_path')
                if not path:
                    continue
                live.add(doc['id'])
                old = conn.execute('SELECT revision_id FROM code_index WHERE document_id=?', (doc['id'],)).fetchone()
                if old and old['revision_id'] == doc['current_revision_id']:
                    continue
                text = self.app.read_text_content(doc['id'])
                lang = PROJECT_EXTENSIONS[PurePosixPath(path).suffix.lower()]
                conn.execute('INSERT OR REPLACE INTO code_index VALUES(?,?,?,?,?,?)',
                    (doc['id'], doc['current_revision_id'], path, lang, text, json.dumps(symbols(text, lang))))
            for row in conn.execute('SELECT document_id FROM code_index').fetchall():
                if row['document_id'] not in live:
                    conn.execute('DELETE FROM code_index WHERE document_id=?', (row['document_id'],))

    def files(self):
        self.refresh()
        with self.app.store.transaction() as conn:
            return [dict(row) | {'symbols': json.loads(row['symbols_json'])} for row in conn.execute(
                'SELECT document_id,revision_id,path,language,symbols_json FROM code_index ORDER BY path')]

    def read(self, path, start=1, end=200):
        path = source_path(path)
        self.refresh()
        with self.app.store.transaction() as conn:
            row = conn.execute('SELECT * FROM code_index WHERE path=?', (path,)).fetchone()
        if row is None:
            raise InvalidInputError('Source file not found')
        if type(start) is not int or type(end) is not int or start < 1 or end < start or end - start > 400:
            raise InvalidInputError('Invalid source line range (maximum 401 lines)')
        lines = row['text'].splitlines()
        return {'path': path, 'document_id': row['document_id'], 'revision_id': row['revision_id'],
                'language': row['language'], 'start_line': start, 'total_lines': len(lines),
                'text': '\n'.join(lines[start - 1:end])[:24000]}

    def search(self, query, limit=12):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
            raise InvalidInputError('Search query must be 1-300 characters')
        self.refresh()
        tokens = list(dict.fromkeys(re.findall(r'[\w.$-]{2,}', query.lower())))[:8] or [query.lower()]
        with self.app.store.transaction() as conn:
            rows = conn.execute('SELECT path,document_id,revision_id,text FROM code_index ORDER BY path').fetchall()
        hits = []
        for row in rows:
            for number, line in enumerate(row['text'].splitlines(), 1):
                score = sum(term in line.lower() for term in tokens)
                if score:
                    hits.append({'path': row['path'], 'document_id': row['document_id'], 'revision_id': row['revision_id'],
                                 'line': number, 'text': line[:800], 'score': score})
        return sorted(hits, key=lambda hit: (-hit['score'], hit['path'], hit['line']))[:min(limit, 30)]

    def write(self, path, text, expected_revision=None):
        path = source_path(path)
        if not isinstance(text, str) or '\x00' in text:
            raise InvalidInputError('Source must be UTF-8 text without NUL')
        raw = text.encode('utf-8')
        self.app._assert_file_size(raw)
        with self.app.store.transaction():
            existing = next((doc for doc in self.app.store.list_documents(self.app.current_project()['id'])
                             if json.loads(doc.get('extra_json') or '{}').get('code_path') == path), None)
            self.refresh()
            with self.app.store.transaction() as conn:
                total = sum(len(row['text'].encode()) for row in conn.execute('SELECT text FROM code_index WHERE path<>?', (path,)))
            if total + len(raw) > 16 * 1024 * 1024:
                raise InvalidInputError('Total indexed source exceeds the 16 MiB safety budget')
            if existing:
                if expected_revision != existing['current_revision_id']:
                    raise ConflictError('Source revision changed; reload before saving')
                anchors = self.app.store.list_nodes(existing['project_id'])
                if any(node.get('document_id') == existing['id'] for node in anchors):
                    raise ConflictError('Remove source-linked nodes before replacing this code file')
                self.app.add_revision(document_id=existing['id'], filename=path.rsplit('/', 1)[-1],
                                      payload=raw, declared_mime='text/plain')
                document = self.app.require_document(existing['id'])
            else:
                if expected_revision is not None:
                    raise ConflictError('Source file no longer exists')
                if len(self.files()) >= 200:
                    raise InvalidInputError('Project source file limit is 200')
                self.app._assert_document_quota(self.app.current_project()['id'])
                document, _ = self.app._store_new_document(project_id=self.app.current_project()['id'],
                    title=path, fmt='txt', source_filename=path.rsplit('/', 1)[-1], payload=raw,
                    resources=(), status='ready', extra={'code_path': path})
            self.refresh()
        return self.app.document_public(document)

    def import_archive(self, raw):
        if len(raw) > self.app.config.limits.max_bytes_per_file:
            raise InvalidInputError('Archive exceeds file size quota')
        entries, total = [], 0
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if len(archive.infolist()) > 400:
                    raise InvalidInputError('Archive has too many entries')
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                        raise InvalidInputError('Symlink and encrypted archive entries are forbidden')
                    path = source_path(info.filename)
                    total += info.file_size
                    if total > 16 * 1024 * 1024 or info.file_size > self.app.config.limits.max_bytes_per_file or len(entries) >= 100:
                        raise InvalidInputError('Expanded code archive exceeds safety limits')
                    text = archive.read(info).decode('utf-8')
                    entries.append((path, text))
        except (zipfile.BadZipFile, UnicodeError, RuntimeError) as exc:
            raise InvalidInputError('Invalid UTF-8 code archive') from exc
        if not entries or len({path for path, _ in entries}) != len(entries):
            raise InvalidInputError('Archive must have unique source paths')
        with self.app.store.transaction():
            if any(path in {f['path'] for f in self.files()} for path, _ in entries):
                raise ConflictError('Archive contains paths already in this project')
            return [self.write(path, text) for path, text in entries]
