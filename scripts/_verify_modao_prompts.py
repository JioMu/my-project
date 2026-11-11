# -*- coding: utf-8 -*-
"""验证墨刀线提示词改动是否真正进入**运行期**链路。

⚠️ 关键点（2026-10-13）：`apps/requirement_analysis/modao_prompts.py:14` 有一个
**模块级 `_CACHE = {}`**，`get_prompt()` 命中后不再读盘。
⇒ 墨刀线提示词是「文件 + 内存缓存」两层，**改完文件必须 restart 容器**才生效。
   只 cp 文件不 restart = 静默用旧提示词，且不报错。

因此本脚本**刻意不直接读盘**，而是调用 `modao_prompts.get_prompt()`
（走的就是运行期那份缓存），才能真正证明「跑生成时读到的就是新提示词」。

容器侧用法（dev 的 scripts 目录已 bind mount，可直接跑；prod 需先 docker cp）：
  docker exec <容器> python /app/scripts/_verify_modao_prompts.py
  docker exec <容器> cat /tmp/_verify_modao_prompts.txt

判据：末尾 `RESULT=SYNCED`（两类 md5 命中期望值 + 锚点串全 OK + 旧句无残留）。
"""
import hashlib
import os
import sys

sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
import django  # noqa: E402

django.setup()

from apps.requirement_analysis import modao_prompts  # noqa: E402

OUT = '/tmp/_verify_modao_prompts.txt'
lines = []


def w(s=''):
    lines.append(s)


def md5(text):
    # 文本口径：universal newlines 会把 \r\n 归一为 \n
    return hashlib.md5(text.encode('utf-8')).hexdigest().upper()


def md5_bin(path):
    # 原始字节口径：与宿主机 Get-FileHash / 容器内 md5sum 一致
    with open(path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest().upper()


# ⚠️ 双口径坑（上一轮 _verify_prompt_sync.py 已踩过一次）：
#    EXPECT 用「原始字节口径」（宿主机 Get-FileHash / 容器 md5sum），
#    而 get_prompt() 返回的是「文本口径」（\r\n → \n）。两者必然不同，
#    若拿文本口径去比对 EXPECT 会 100% 误报 MISMATCH。
#    ⇒ 正确做法：文件按 bin 口径比对 EXPECT；缓存按 text 口径与「文件文本口径」比对。
# 期望值 = 当前仓库提示词文件的 md5（Get-FileHash MD5，原始字节口径）
# ⚠️ 每次改动提示词后必须同步更新本表，否则 STEP5/STEP6 会 MISMATCH（非真故障）。
# 2026-10-14 更新：p2/p3 补「兜底句先改写 + 覆盖不回退 + 硬约束6例外」；
#                  p7 补「pass_criteria 原样继承」（修根因：判据在合并阶段被静默丢弃）。
# 2026-11-05 更新：p0 补硬约束 29/30/31（模块=主画布+多状态 / 状态差异各自成 key_features
#                  / 画布边界不可越界）；p2 补 feature_ref、api_ref 强制字段；
#                  p7 补 ref 并集透传 + category 保真（commit 0ae6a19）。
EXPECT = {
    'p0_requirement_struct': '809B3460D76C2197CC32A6A91F1BABA4',
    'p2_testpoint_gen': 'B042C48309C0BA93DAB731A0EC097F53',
    'p3_case_gen': '6596C958C826F7003F7003197AB8B428',
    'p7_merge_dedup': '9EB29E57F4E624BE029EECC35ADBB9C1',
}

# 锚点串：取自提示词文件真实文本（⚠️ 不要凭印象写，曾误报过 MISSING）
FEATURES = {
    'p2_testpoint_gen': [
        ('兜底句不得单独成条', '兜底句不得单独成条'),
        ('功能级可观测判据', '功能级可观测判据'),
        ('硬约束9 历史缺陷高发维度', '历史缺陷高发维度'),
        ('① 再次执行', '① **再次执行**'),
        ('② 刷新与回撤', '② **刷新与回撤**'),
        ('③ 权益边界', '③ **权益边界**'),
        ('④ 中断恢复', '④ **中断恢复**'),
    ],
    'p0_requirement_struct': [
        ('ui_screen降噪', 'ui_screen 底图上的未批注元素'),
        ('批注增量优先', '批注增量优先（Annotation-First）'),
        ('原型底模降噪', '原型底模降噪'),
        ('负向禁止静态底模', '静态背景文字'),
    ],
    'p3_case_gen': [
        ('兜底句不得单独成条', '兜底句不得单独成条'),
        ('功能级可观测断言', '功能级可观测断言'),
        ('硬约束8 历史缺陷高发维度', '历史缺陷高发维度'),
        ('① 再次执行', '① **再次执行**'),
        ('② 刷新与回撤', '② **刷新与回撤**'),
        ('③ 权益边界', '③ **权益边界**'),
    ],
    'p7_merge_dedup': [
        ('pass_criteria 原样继承', 'pass_criteria 必须原样继承'),
        ('功能级可观测判据', '功能级可观测判据'),
        ('禁止改写为兜底句', '禁止把已有判据改写'),
    ],
}

# 旧句残留检查：改动前这些表述把视觉兜底句当成了完整预期结果，
# 必须确认它们已从「允许单独成条」的语境里删掉。
# 注意：新文本里仍会以「禁止项」形式引用这些词（p3:25），所以只查带 "即可" 的原句。
LEGACY = {
    'p0_requirement_struct': [],
    'p2_testpoint_gen': ['无白屏/破图/资源缺失即可'],
    'p3_case_gen': ['无白屏/破图/资源缺失即可'],
    'p7_merge_dedup': [],
}

bad = 0
w('=' * 70)
w('墨刀线提示词运行期校验（走 modao_prompts.get_prompt，含 _CACHE）')
w('=' * 70)

PROMPTS_DIR = '/app/apps/requirement_analysis/modao_skill/prompts'

for name in ('p0_requirement_struct', 'p2_testpoint_gen', 'p3_case_gen', 'p7_merge_dedup'):
    path = os.path.join(PROMPTS_DIR, name + '.md')
    text = modao_prompts.get_prompt(name)  # ← 走缓存，等同运行期实际取到的内容

    # ① 文件本身对不对（bin 口径 ↔ EXPECT）
    fbin = md5_bin(path)
    ok_file = (fbin == EXPECT[name])
    if not ok_file:
        bad += 1

    # ② 运行期缓存是不是新的（cache text 口径 ↔ 文件 text 口径）
    ftext = md5(open(path, encoding='utf-8').read())
    ctext = md5(text)
    ok_cache = (ctext == ftext)
    if not ok_cache:
        bad += 1

    w('')
    w('[%s] len(cache)=%d' % (name, len(text)))
    w('       file  bin-md5=%s  %s' % (fbin, 'OK' if ok_file
                                       else 'MISMATCH(expect %s)' % EXPECT[name]))
    w('       cache text-md5=%s  file text-md5=%s  %s'
      % (ctext, ftext, 'OK(缓存已刷新)' if ok_cache else 'STALE(仍用旧缓存)'))
    w('       cache_keys=%s' % sorted(modao_prompts._CACHE.keys()))

    for label, needle in FEATURES[name]:
        hit = needle in text
        if not hit:
            bad += 1
        w('       feature %-26s %-14s %s' % (label, needle, 'OK' if hit else 'MISSING'))

    for needle in LEGACY[name]:
        left = needle in text
        if left:
            bad += 1
        w('       legacy  %-26s %-14s %s'
          % ('旧兜底句残留', needle, 'STILL THERE' if left else 'REMOVED'))

w('')
w('RESULT=%s' % ('SYNCED' if bad == 0 else 'FAILED(%d)' % bad))

with open(OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\n')
