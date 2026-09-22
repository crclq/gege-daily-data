#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端版三方同步（GitHub Actions 里跑，不依赖本机开机）。

做的事
------
1. 从 Supabase 拉取各表（分页）
2. 从本仓库 data/ 拉取现有备份
3. 按主键取并集，同主键比较 updated_at 保留最新整行
4. 把合并结果写回 data/*.json（commit 回仓库）
5. 把库里缺的行 upsert 回 Supabase（可跳过）

与本机 _sync_all.py 的区别
--------------------------
- 不写本机 local_store（云上没有持久磁盘，写了也没意义）
- 密钥全部从环境变量读，不落盘
- 只同步公开安全表；todos / course_cells 由 vanward-calendar 的私有仓库流程负责

环境变量
--------
GH_TOKEN      GitHub PAT（写本仓库用）；缺省回退到 GITHUB_TOKEN
SUPA_KEY      Supabase publishable key
SKIP_DB_WRITE 设为 1 则只备份不回写数据库（默认回写）
"""
import base64
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REPO = 'crclq/gege-daily-data'
BRANCH = 'main'
API = 'https://api.github.com'
SUPA = 'https://rduogcxrhhqhfkhwcnpt.supabase.co'

GH_TOKEN = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
SUPA_KEY = os.environ.get('SUPA_KEY') or ''
SKIP_DB_WRITE = os.environ.get('SKIP_DB_WRITE', '') == '1'

RETRY_MAX = 20
PAGE = 1000

GH_HDR = {
    'Authorization': 'Bearer ' + GH_TOKEN,
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'gege-daily-actions',
    'Content-Type': 'application/json',
    'Connection': 'close',
}
SB_HDR = {
    'apikey': SUPA_KEY, 'Authorization': 'Bearer ' + SUPA_KEY,
    'User-Agent': 'gege-daily-actions', 'Connection': 'close',
}

# (表名, 主键, Git路径, 时间戳字段, 是否回写库)
TABLES = [
    ('video_dictation',        'id', 'data/dictation.json',        ('updated_at', 'record_at', 'created_at'), True),
    ('video_dictation_ai_log', 'id', 'data/dictation_ai_log.json', ('created_at',),                           False),
    ('video_card',             'id', 'data/video_card.json',       ('updated_at', 'created_at'),              True),
    ('video_recommend',        'id', 'data/video_recommend.json',  ('checked_at', 'created_at'),              True),
]

SKIP_COLS = {'video_dictation': ('char_count',)}


def req(url, headers, method='GET', data=None, retry=RETRY_MAX, timeout=90):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    last = None
    for i in range(retry):
        try:
            rq = urllib.request.Request(url, data=body, method=method, headers=headers)
            r = urllib.request.urlopen(rq, timeout=timeout)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return e.code, e.read()
            last = e
        except Exception as e:
            last = e
        time.sleep(min(0.8 * (i + 1), 3.0))
    return None, repr(last).encode('utf-8', 'ignore')


def gh_get_raw(path, retry=8):
    h = dict(GH_HDR)
    h['Accept'] = 'application/vnd.github.raw'
    st, body = req('%s/repos/%s/contents/%s?ref=%s' % (API, REPO, path, BRANCH), h, retry=retry)
    return body if st == 200 else None


def gh_put(path, content_bytes, message):
    url = '%s/repos/%s/contents/%s?ref=%s' % (API, REPO, path, BRANCH)
    st, body = req(url, GH_HDR, 'GET', retry=8)
    sha = None
    if st == 200:
        try:
            sha = json.loads(body.decode('utf-8')).get('sha')
        except Exception:
            sha = None
    elif st != 404:
        return st, body[:200].decode('utf-8', 'ignore')
    payload = {'message': message, 'branch': BRANCH,
               'content': base64.b64encode(content_bytes).decode('ascii')}
    if sha:
        payload['sha'] = sha
    return req(url.split('?')[0], GH_HDR, 'PUT', payload, retry=8)


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


def main():
    if not GH_TOKEN or not SUPA_KEY:
        print('缺少 GH_TOKEN / SUPA_KEY，跳过本次同步（请在仓库 Settings → Secrets 里配置）。')
        return 0

    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    print('云端同步开始 %s (UTC)' % now)
    changed = 0

    for table, pk, gpath, tsfields, write_db in TABLES:
        print('\n── %s' % table)
        db_ok = True
        try:
            db_rows = sb_get_all(table)
        except Exception as e:
            db_rows, db_ok = [], False
            print('  [!!] Supabase 读取失败：%r' % e)

        gh_rows = []
        b = gh_get_raw(gpath)
        if b:
            try:
                j = json.loads(b.decode('utf-8'))
                gh_rows = j.get('rows') if isinstance(j, dict) else j
            except Exception:
                gh_rows = []
        print('  库 %-5s  Git %-5d' % (len(db_rows) if db_ok else '失败', len(gh_rows)))

        merged, order = {}, []
        for rows in (db_rows, gh_rows):
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
        st, resp = gh_put(gpath, raw, 'sync: %s %d rows (%s UTC)' % (table, len(out), now))
        if st in (200, 201):
            print('  已推 Git %-28s %8d bytes' % (gpath, len(raw)))
            changed += 1
        else:
            print('  [FAIL] Git %s HTTP %s %s' % (gpath, st, (resp or b'')[:120].decode('utf-8', 'ignore')))

        if write_db and not SKIP_DB_WRITE:
            if not db_ok:
                print('  跳过补库：库本次读不到')
            elif add_db:
                n, msg = sb_upsert(table, [merged[k] for k in add_db], pk)
                print('  补库 %d 行 -> %s' % (n, msg))
        elif write_db:
            print('  跳过补库（SKIP_DB_WRITE=1）')

    print('\n云端同步完成，更新 %d 个文件' % changed)
    return 0


if __name__ == '__main__':
    sys.exit(main())
