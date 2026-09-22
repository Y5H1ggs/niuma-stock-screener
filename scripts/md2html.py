#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""md2html.py — 把 Markdown 渲染成**自包含 HTML**（纯标准库，零依赖）

为什么需要它：
  本项目对外交付走的是本地文件，而 `.md` 在很多预览器里**只作为文件列出、不给渲染预览**
  （支持预览的通常只有 `.html`）。于是"文档打不开/点不了"的问题反复出现。
  本脚本把 .md 转成一个**单文件、无外链依赖**的 HTML：
    · 可直接在浏览器打开、可在内置预览面板里看
    · 每段代码块右上角带「复制」按钮 —— 命令**真的可以点**
    · 打印/导出 PDF 也正常

用法：
    python md2html.py README.md                 # 生成 README.html
    python md2html.py README.md -o 说明.html
    python md2html.py README.md CHANGELOG.md    # 批量（各自生成同名 .html）
"""
import html
import io
import os
import re
import sys

CSS = """
:root{--ink:#141C28;--ink2:#3B4757;--muted:#7C8798;--line:#E6E1D8;--card:#FFFFFF;
      --bg:#F7F5F0;--gold:#A9843F;--goldsoft:#F7F1E4;--goldline:#E7D9B8;
      --codebg:#F4F2EC;--codeink:#2B3646;}
*{box-sizing:border-box;}
body{margin:0;padding:36px 20px 80px;background:var(--bg);color:var(--ink);
     font:15px/1.85 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
     "Hiragino Sans GB","Microsoft YaHei",sans-serif;}
.wrap{max-width:920px;margin:0 auto;background:var(--card);border:1px solid var(--line);
      border-radius:16px;padding:42px 46px 54px;
      box-shadow:0 1px 1px rgba(18,36,58,.04),0 14px 34px -22px rgba(18,36,58,.20);}
h1{font-size:29px;margin:0 0 18px;letter-spacing:.4px;}
h2{font-size:22px;margin:38px 0 14px;padding-left:12px;position:relative;letter-spacing:.3px;}
h2::before{content:"";position:absolute;left:0;top:5px;bottom:5px;width:4px;
           border-radius:2px;background:var(--gold);}
h3{font-size:17px;margin:26px 0 10px;color:var(--ink2);}
h4{font-size:15px;margin:20px 0 8px;color:var(--ink2);}
p{margin:10px 0;}
a{color:#1B4A73;}
code{background:var(--codebg);color:var(--codeink);border-radius:4px;padding:1.5px 6px;
     font-size:13px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
pre{position:relative;background:#0F2237;color:#DCE6F2;border-radius:11px;
    padding:16px 18px;overflow:auto;margin:14px 0;
    font:13px/1.75 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
pre code{background:none;color:inherit;padding:0;font-size:13px;}
.copy{position:absolute;top:9px;right:10px;border:1px solid rgba(255,255,255,.28);
      background:rgba(255,255,255,.10);color:#DCE6F2;border-radius:7px;
      padding:3px 11px;font-size:12px;cursor:pointer;font-family:inherit;
      transition:.15s;}
.copy:hover{background:rgba(255,255,255,.22);}
.copy.ok{background:rgba(126,217,168,.24);border-color:rgba(126,217,168,.5);}
table{border-collapse:collapse;width:100%;margin:16px 0;font-size:14px;}
th,td{border:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top;}
th{background:var(--goldsoft);color:#6B5320;font-weight:600;}
tr:nth-child(even) td{background:#FCFBF8;}
blockquote{margin:16px 0;padding:12px 18px;background:var(--goldsoft);
           border-left:4px solid var(--gold);border-radius:0 10px 10px 0;
           color:#5C4A22;}
blockquote p{margin:6px 0;}
ul,ol{margin:12px 0;padding-left:26px;}
li{margin:5px 0;}
hr{border:none;border-top:1px solid var(--line);margin:34px 0;}
.anchor{font-size:12px;color:var(--muted);margin-bottom:22px;}
"""

JS = """
document.querySelectorAll('pre').forEach(function(pre){
  var b=document.createElement('button');
  b.className='copy'; b.type='button'; b.textContent='复制';
  b.addEventListener('click',function(){
    var t=pre.querySelector('code');
    var s=t?t.innerText:pre.innerText;
    var done=function(){b.textContent='已复制';b.classList.add('ok');
      setTimeout(function(){b.textContent='复制';b.classList.remove('ok');},1400);};
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(s).then(done,function(){done();});
    }else{
      var ta=document.createElement('textarea');ta.value=s;document.body.appendChild(ta);
      ta.select();try{document.execCommand('copy');}catch(e){}document.body.removeChild(ta);done();
    }
  });
  pre.appendChild(b);
});
"""


def inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', s)
    s = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', s)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', s)
    return s


def render(md):
    lines = md.replace('\r\n', '\n').split('\n')
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        # 代码围栏
        if ln.strip().startswith('```'):
            lang = ln.strip()[3:].strip()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                buf.append(lines[i]); i += 1
            i += 1
            body = html.escape('\n'.join(buf))
            out.append('<pre><code class="lang-%s">%s</code></pre>' % (lang, body))
            continue
        # 表格
        if ln.strip().startswith('|') and i + 1 < len(lines) and \
           re.match(r'^\s*\|[\s:|-]+\|\s*$', lines[i + 1]):
            head = [c.strip() for c in ln.strip().strip('|').split('|')]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
                i += 1
            t = ['<table><thead><tr>'] + ['<th>%s</th>' % inline(c) for c in head] + \
                ['</tr></thead><tbody>']
            for r in rows:
                t.append('<tr>' + ''.join('<td>%s</td>' % inline(c) for c in r) + '</tr>')
            t.append('</tbody></table>')
            out.append(''.join(t))
            continue
        # 标题
        m = re.match(r'^(#{1,6})\s+(.*)$', ln)
        if m:
            lv = len(m.group(1))
            out.append('<h%d>%s</h%d>' % (lv, inline(m.group(2)), lv))
            i += 1
            continue
        # 分隔线
        if re.match(r'^\s*([-*_])\s*\1\s*\1[\s\1]*$', ln):
            out.append('<hr>')
            i += 1
            continue
        # 引用
        if ln.strip().startswith('>'):
            buf = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                buf.append(lines[i].strip()[1:].strip()); i += 1
            out.append('<blockquote>' + ''.join('<p>%s</p>' % inline(x) for x in buf if x) +
                       '</blockquote>')
            continue
        # 列表
        if re.match(r'^\s*([-*+]|\d+\.)\s+', ln):
            ordered = bool(re.match(r'^\s*\d+\.\s+', ln))
            tag = 'ol' if ordered else 'ul'
            items = []
            while i < len(lines) and re.match(r'^\s*([-*+]|\d+\.)\s+', lines[i]):
                items.append(re.sub(r'^\s*([-*+]|\d+\.)\s+', '', lines[i]))
                i += 1
            out.append('<%s>%s</%s>' % (tag, ''.join('<li>%s</li>' % inline(x) for x in items), tag))
            continue
        if not ln.strip():
            i += 1
            continue
        # 段落
        buf = []
        while i < len(lines) and lines[i].strip() and \
              not re.match(r'^(#{1,6}\s|```|\||>|\s*([-*+]|\d+\.)\s)', lines[i]):
            buf.append(lines[i]); i += 1
        out.append('<p>%s</p>' % inline(' '.join(buf)))
    return '\n'.join(out)


def convert(src, dst=None):
    dst = dst or os.path.splitext(src)[0] + '.html'
    md = open(src, encoding='utf-8').read()
    title = os.path.splitext(os.path.basename(src))[0]
    m = re.search(r'^#\s+(.+)$', md, re.M)
    if m:
        title = re.sub(r'[`*]', '', m.group(1)).strip()
    doc = ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n'
           '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
           '<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n<div class="wrap">\n'
           '<div class="anchor">由 %s 渲染 · md2html.py</div>\n%s\n</div>\n'
           '<script>%s</script>\n</body>\n</html>\n'
           % (html.escape(title), CSS, html.escape(os.path.basename(src)), render(md), JS))
    open(dst, 'w', encoding='utf-8').write(doc)
    return dst, len(doc)


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    out = None
    for j, a in enumerate(sys.argv):
        if a == '-o' and j + 1 < len(sys.argv):
            out = sys.argv[j + 1]
    if not args:
        raise SystemExit('用法：python md2html.py <文件.md> [-o 输出.html]')
    for j, f in enumerate(args):
        d, n = convert(f, out if (out and len(args) == 1) else None)
        print('  %s -> %s  (%.1f KB)' % (f, d, n / 1024))
