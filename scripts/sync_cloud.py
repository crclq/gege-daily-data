#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端版三方同步（GitHub Actions 里跑，不依赖本机开机）。

为什么用 git 提交而不是 GitHub API
----------------------------------
Actions 的默认 GITHUB_TOKEN **不会自动成为环境变量**，必须在 workflow 里显式写
`GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` 才有。而改 workflow 文件需要 PAT 的
`workflow` scope（我们的 token 只有 repo），网页打不开时也没法改。
但 runner 已经用该 token 配好了 git 凭据（checkout 时写入 .extraheader，
见日志 "Setting up auth"），所以 **直接 git commit + push 就行，一个 token secret 都不需要**。

流程
----
1. 从 Supabase 拉取各表（分页）
2. 读仓库里已有的 data/*.json（checkout 已拉下来）
3. 按主键取并集，同主键比较 updated_at 保留最新整行
4. 写回文件 → git commit → git push
5. 把库里缺的行 upsert 回 Supabase

环境变量
--------
SUPA_KEY      Supabase publishable key（唯一必需的 secret）
SKIP_DB_WRITE 设为 1 则只备份不回写数据库（默认回写）
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

SUPA = 'https://rduogcxrhhqhfkhwcnpt.supabase.co'
SUPA_KEY = os.environ.get('SUPA_KEY') or ''
SKIP_DB_WRITE = os.environ.get('SKIP_DB_WRITE', '') == '1'
WS = os.environ.get('GITHUB_WORKSPACE') or os.getcwd()

RETRY_MAX = 20
PAGE = 1000

SB_HDR = {
    'apikey': SUPA_KEY, 'Authorization': 'Bearer ' + SUPA_KEY,
    'User-Agent': 'gege-daily-actions', 'Connection': 'close',
}

# (表名, 主键, 仓库内相对路径, 时间戳字段, 是否回写库)
TABLES = [
    ('video_dictation',        'id', 'data/dictation.json',        ('updated_at', 'record_at', 'created_at'), True),
    ('video_dictation_ai_log', 'id', 'data/dictation_ai_log.json', ('created_at',),                           False),
    ('video_card',             'id', 'data/video_card.json',       ('updated_at', 'created_at'),              True),
    ('video_recommend',        'id', 'data/video_recommend.json',  ('checked_at', 'created_at'),              True),
]

SKIP_COLS = {'video_dictation': ('char_count',)}


def req(url, headers, method='GET', data=None, retry=RETRY_MAX, timeout=90):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    for i in range(retry):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url, data=body, method=method, headers=headers), timeout=timeout)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return e.code, e.read()
        except Exception:
            pass
        time.sleep(min(0.8 * (i + 1), 3.0))
    return None, b'TIMEOUT'


def sb_get_all(table):
    out, start = [], 0
    while True:
        h = dict(SB_HDR)
        h['Range'] = '%d-%d' % (start, start + PAGE - 1)
        st, body = req('%s/rest/v1/%s?select=*' % (SUPA, table), h)
        if st != 200:
            raise RuntimeError('读 %s 失败 HTTP %s %s' % (table, st, body[:160]))
        rows = json.loads(body.decode('utf-8'))
        out.extend(rows)
        if len(rows) < PAGE or start > 20000:
            break
        start += PAGE
    return out


def sb_upsert(table, rows, pk):
    if not rows:
        return 0, '无需写入'
    url = '%s/rest/v1/%s?on_conflict=%s' % (SUPA, table, pk)
    h = dict(SB_HDR)
    h['Content-Type'] = 'application/json'
    h['Prefer'] = 'resolution=merge-duplicates,return=minimal'
    drop = set(SKIP_COLS.get(table, ()))
    done, B = 0, 200
    while done < len(rows):
        chunk = [dict(r) for r in rows[done:done + B]]
        for c in drop:
            for r in chunk:
                r.pop(c, None)
        for _ in range(6):
            st, body = req(url, h, 'POST', chunk)
            if st in (200, 201, 204):
                break
            txt = body[:400].decode('utf-8', 'ignore')
            m = re.search(r'Column\s+"([^"]+)"\s+is\s+a\s+generated\s+column', txt)
            if st == 400 and m and m.group(1) not in drop:
                drop.add(m.group(1))
                print('      （剔除生成列 %s 后重试）' % m.group(1))
                for r in chunk:
                    r.pop(m.group(1), None)
                continue
            return done, 'HTTP %s %s' % (st, txt[:200])
        done += len(chunk)
    return done, 'OK'


def ts_of(row, fields):
    for f in fields:
        v = row.get(f)
        if v:
            return str(v)
    return ''


def mask_name(s):
    if not s or not isinstance(s, str):
        return s
    s = s.strip()
    return s if len(s) <= 1 else s[0] + '*' * (len(s) - 1)


def git(*args, check=False):
    p = subprocess.run(['git'] + list(args), cwd=WS,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode('utf-8', 'ignore')
    if check and p.returncode != 0:
        raise RuntimeError('git %s 失败：%s' % (' '.join(args), out[-300:]))
    return p.returncode, out


def main():
    if not SUPA_KEY:
        print('缺少 SUPA_KEY，跳过本次同步（需在仓库 Settings → Secrets → Actions 里配置）。')
        return 0

    now = time.strftime('%Y-%m-%d %H:%M', time.gmtime())
    print('云端同步开始 %s UTC   工作目录 %s' % (now, WS))

    wrote = []
    for table, pk, rel, tsfields, write_db in TABLES:
        print('\n── %s' % table)
        db_ok = True
        try:
            db_rows = sb_get_all(table)
        except Exception as e:
            db_rows, db_ok = [], False
            print('  [!!] Supabase 读取失败：%r' % e)

        path = os.path.join(WS, rel)
        old_rows = []
        if os.path.exists(path):
            try:
                j = json.load(open(path, encoding='utf-8'))
                old_rows = j.get('rows') if isinstance(j, dict) else j
            except Exception:
                old_rows = []
        print('  库 %-5s  仓库 %-5d' % (len(db_rows) if db_ok else '失败', len(old_rows)))

        merged, order = {}, []
        for rows in (db_rows, old_rows):
            for r in rows:
                k = str(r.get(pk))
                if not k or k == 'None':
                    continue
                if k not in merged:
                    merged[k] = r
                    order.append(k)
                elif ts_of(r, tsfields) > ts_of(merged[k], tsfields):
                    merged[k] = r
        mrows = [merged[k] for k in order]
        add_db = sorted(set(merged) - {str(r.get(pk)) for r in db_rows})
        print('  合并后 %-5d   库缺 %d' % (len(mrows), len(add_db)))

        out = list(mrows)
        if table == 'video_dictation':
            for r in out:
                if r.get('child_name'):
                    r['child_name'] = mask_name(r['child_name'])
        bundle = {'generated': now + ' UTC', 'count': len(out),
                  'source': 'supabase:public.' + table, 'rows': out}
        raw = json.dumps(bundle, ensure_ascii=False, indent=1).encode('utf-8')

        old_raw = open(path, 'rb').read() if os.path.exists(path) else b''
        if raw == old_raw:
            print('  内容无变化，跳过写文件')
        else:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            open(path, 'wb').write(raw)
            wrote.append(rel)
            print('  已写 %s  %d bytes' % (rel, len(raw)))

        if write_db and not SKIP_DB_WRITE:
            if not db_ok:
                print('  跳过补库：库本次读不到')
            elif add_db:
                n, msg = sb_upsert(table, [merged[k] for k in add_db], pk)
                print('  补库 %d 行 -> %s' % (n, msg))
        elif write_db:
            print('  跳过补库（SKIP_DB_WRITE=1）')

    if not wrote:
        print('\n没有文件变化，无需提交')
        return 0

    br = os.environ.get('GITHUB_REF_NAME') or 'main'
    git('config', 'user.name', 'gege-sync')
    git('config', 'user.email', 'sync@gege.local')
    git('add', *wrote, check=True)
    rc, _ = git('diff', '--cached', '--quiet')
    if rc == 0:
        print('\n暂存区无差异，跳过提交')
        return 0
    git('commit', '-m', 'sync: %s (%s UTC)' % (','.join(wrote), now), check=True)
    rc, out = git('push', 'origin', 'HEAD:%s' % br)
    if rc != 0:
        print('push 失败：', out[-500:])
        return 1
    print('\n已提交并推送 %d 个文件到 %s' % (len(wrote), br))
    return 0


if __name__ == '__main__':
    sys.exit(main())
