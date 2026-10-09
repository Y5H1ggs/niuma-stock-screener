"""make_release.py — 把指定版本的 GitHub Release 建好/更新好。

设计要点（避免"Release 与 CHANGELOG 各说各话"）：
  ★ **Release 正文直接从 `CHANGELOG.md` 提取该版本段落** —— 单一来源，
    不手工复制、不会漂移。CHANGELOG 改了就重跑本脚本即可。

用法：
  python scripts/make_release.py v1.2.17
  python scripts/make_release.py v1.2.17 --dry      # 只打印正文，不调 API

前置：
  · 本机无 `gh`，直接走 GitHub API（**api.github.com 直连可通，不必走 git 那个代理**）
  · token：`git credential fill` 取（脚本自动调用），或设环境变量 GITHUB_TOKEN
  · tag 必须已推送（否则 API 会报 422）
"""
import io, json, os, re, subprocess, sys, urllib.error, urllib.request

REPO_FALLBACK = 'Y5H1ggs/niuma-stock-screener'


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ''


def repo_from_git():
    url = sh(['git', 'remote', 'get-url', 'origin']).strip()
    m = re.search(r'github\.com[:/]([\w.\-]+/[\w.\-]+?)(?:\.git)?$', url)
    return m.group(1) if m else REPO_FALLBACK


def body_from_changelog(tag):
    """从 CHANGELOG.md 提取 `## <tag> …` 起、到下一个 `## ` 之前的内容。"""
    if not os.path.exists('CHANGELOG.md'):
        return None
    s = io.open('CHANGELOG.md', encoding='utf-8').read()
    pat = re.compile(r'^## (%s\b[^\n]*)\n(.*?)(?=^## |\Z)' % re.escape(tag), re.M | re.S)
    m = pat.search(s)
    if not m:
        return None
    title, body = m.group(1).strip(), m.group(2).strip()
    body = body.rstrip().rstrip('-').rstrip()
    return '## %s\n\n%s\n\n---\n\n> 完整版本沿革见 [CHANGELOG.md](https://github.com/%s/blob/main/CHANGELOG.md)\n' % (
        title, body, REPO)


def call(url, data=None, method='GET', tk=''):
    req = urllib.request.Request(url, data=data, method=method)
    if tk:
        req.add_header('Authorization', 'token %s' % tk)
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('User-Agent', 'niuma-release')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8') or '{}')
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {'message': '%s: %s' % (type(e).__name__, e)}


args = [a for a in sys.argv[1:] if not a.startswith('--')]
DRY = '--dry' in sys.argv
if not args:
    print(__doc__)
    sys.exit(1)
TAG = args[0]

REPO = repo_from_git()
print('  仓库:', REPO)
print('  版本:', TAG)

body = body_from_changelog(TAG)
if not body:
    print('  ❌ CHANGELOG.md 里找不到 %s 的段落 —— 先补 CHANGELOG，再来发 Release。' % TAG)
    sys.exit(1)
print('  ✓ 已从 CHANGELOG 提取正文（%d 字符）' % len(body))

if DRY:
    print('\n' + '=' * 90)
    print(body)
    sleep_note = ''
    print('=' * 90)
    print('(--dry：未调用 API)')
    sys.exit(0)

TK = os.environ.get('GITHUB_TOKEN') or ''
if not TK:
    # fallback：脚本内调用 `git credential fill`（需要在 stdin 喂协议信息）。
    # ⚠️ 本机实测在**子进程里**取不到（返回空）—— 而 bash 管道下是可行的。
    #   所以用法优先传环境变量；这里只是兜底，并打印调试信息便于定位。
    try:
        p = subprocess.run(['git', 'credential', 'fill'],
                           input='protocol=https\nhost=github.com\n\n',
                           capture_output=True, text=True, timeout=20)
        for line in p.stdout.splitlines():
            if line.startswith('password='):
                TK = line.split('=', 1)[1].strip()
        if not TK:
            print('  [debug] git credential 返回空；stdout=%r stderr=%r'
                  % (p.stdout[:120], p.stderr[:120]))
    except Exception as e:
        print('  [debug] git credential 调用异常:', e)
if not TK:
    print('  ❌ 取不到 token。请改用环境变量传入：')
    print('     TOKEN=$(printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill \\')
    print('              | grep ^password= | cut -d= -f2)')
    print('     GITHUB_TOKEN=$TOKEN python scripts/make_release.py %s' % TAG)
    sys.exit(1)
print('  ✓ token 已取到（%d 字符）' % len(TK))

payload = json.dumps({
    'tag_name': TAG,
    'name': TAG,
    'body': body,
    'draft': False,
    'prerelease': False,
}, ensure_ascii=False).encode('utf-8')

st, d = call('https://api.github.com/repos/%s/releases/tags/%s' % (REPO, TAG), tk=TK)
if st == 200:
    print('  已存在 Release id=%s → 更新（PATCH）' % d.get('id'))
    st2, d2 = call('https://api.github.com/repos/%s/releases/%s' % (REPO, d['id']),
                   payload, 'PATCH', TK)
else:
    print('  无同 tag Release（HTTP %s）→ 新建（POST）' % st)
    st2, d2 = call('https://api.github.com/repos/%s/releases' % REPO, payload, 'POST', TK)

if st2 in (200, 201):
    print('  ✅ Release 就绪')
    print('     name     : %s' % d2.get('name'))
    print('     tag_name : %s' % d2.get('tag_name'))
    print('     html_url : %s' % d2.get('html_url'))
else:
    print('  ❌ 失败 HTTP %s' % st2)
    print('     ', json.dumps(d2, ensure_ascii=False)[:400])
    sys.exit(1)
