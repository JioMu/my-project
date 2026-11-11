# Rebuild TestHub PRODUCTION backend image.
# Root fix for: docker-cp'ed .py files are lost whenever the container is recreated,
# because /app/apps is NOT a bind mount (code comes from the image only).
#
# Usage:  powershell -ExecutionPolicy Bypass -File C:\TestHub\scripts\rebuild_prod_backend.ps1
# Notes:  ASCII only, CRLF required. Stops immediately on the first failed step.

$ErrorActionPreference = 'Stop'
$Repo = 'C:\TestHub'
$LogDir = Join-Path $Repo 'logs'
$Stamp = Get-Date -Format 'yyyyMMdd_HHmm'
$Log = Join-Path $LogDir "rebuild_prod_$Stamp.log"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Say($m) {
  $t = Get-Date -Format 'HH:mm:ss'
  $line = "$t $m"
  Write-Output $line
  Add-Content -Path $Log -Value $line -Encoding UTF8
}

function Fail($m) {
  Say "FATAL: $m"
  Say '=== ABORTED, no further step executed ==='
  exit 1
}

Say '=== STEP 0 precheck ==='
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'docker command not found' }
Set-Location $Repo
Say "repo=$Repo"

Say '--- baseline md5 (prod modao_workflow.py) ---'
docker exec testhub-backend md5sum /app/apps/requirement_analysis/modao_workflow.py
Say "baseline_exit=$LASTEXITCODE"

Say '--- baseline md5 (2026-10-13 prompt + zentao coverage fix) ---'
docker exec testhub-backend md5sum /app/tester.md /app/tester_pro.md /app/apps/requirement_analysis/independent_skill/prompts/tester.md /app/apps/requirement_analysis/independent_skill/prompts/tester_pro.md /app/apps/zentao/services.py

Say '--- baseline md5 (2026-10-14 modao p2/p3/p7 prompt) ---'
docker exec testhub-backend md5sum /app/apps/requirement_analysis/modao_skill/prompts/p2_testpoint_gen.md /app/apps/requirement_analysis/modao_skill/prompts/p3_case_gen.md /app/apps/requirement_analysis/modao_skill/prompts/p7_merge_dedup.md
Say '--- baseline md5 (2026-10-15 modao p0 底模降噪 prompt) ---'
docker exec testhub-backend md5sum /app/apps/requirement_analysis/modao_skill/prompts/p0_requirement_struct.md
Say '--- baseline anchors (old fallback sentence should be 1 before sync; new dimension 0) ---'
docker exec testhub-backend sh -c "grep -c '历史缺陷高发维度' /app/apps/requirement_analysis/modao_skill/prompts/p2_testpoint_gen.md /app/apps/requirement_analysis/modao_skill/prompts/p3_case_gen.md"

Say '--- recent modao tasks ---'
docker exec testhub-backend python -c "import os,sys,django;sys.path.insert(0,'/app');os.environ.setdefault('DJANGO_SETTINGS_MODULE','backend.settings');django.setup();from apps.requirement_analysis.models import ModaoPrototype;print(ModaoPrototype.objects.filter(status__in=['running','processing','extracting']).count());print(list(ModaoPrototype.objects.order_by('-id').values_list('id','status','current_stage')[:3]))"

Say '=== STEP 1 backup ==='
$bk = "/tmp/rollback_$Stamp"
docker exec testhub-backend mkdir -p $bk
if ($LASTEXITCODE -ne 0) { Fail 'mkdir backup dir failed' }
docker exec testhub-backend tar -czf "$bk/apps.tar.gz" -C /app apps backend
if ($LASTEXITCODE -ne 0) { Fail 'tar backup failed' }
Say "container backup -> $bk/apps.tar.gz"

$mp = (docker exec testhub-mysql printenv MYSQL_ROOT_PASSWORD).Trim()
Say "mysql root password length=$($mp.Length)"
docker exec testhub-mysql mysqldump -uroot "-p$mp" --result-file=/tmp/testhub_dump.sql testhub
if ($LASTEXITCODE -ne 0) { Fail 'mysqldump failed' }
$dumpDir = Join-Path $Repo '_db_backup'
if (-not (Test-Path $dumpDir)) { New-Item -ItemType Directory -Path $dumpDir -Force | Out-Null }
docker cp testhub-mysql:/tmp/testhub_dump.sql (Join-Path $dumpDir "testhub_$Stamp.sql")
Say "db dump -> $dumpDir\testhub_$Stamp.sql"

Say '=== STEP 1.5 sync host code into /app (survives nothing; bake happens in STEP 2) ==='
# The image is built from the HOST worktree (Dockerfile.backend: COPY . .), so any
# code that currently lives ONLY inside the container (docker cp hot-fix) would be
# silently lost by the rebuild. Optionally run scripts/_prod_bake.sync.ps1 first if
# you need to copy container-only changes back to the host before this point.
Say 'host is the source of truth for STEP 2; verify with: git status --short'

Say '=== STEP 2 docker compose build backend (40-70 min, no --no-cache) ==='
# 2026-10-10: build output MUST be captured. Previously it went to the (hidden) console only,
# so two consecutive pip failures left us with no error text at all and cost ~2h of guessing.
$BuildLog = Join-Path $LogDir "build_$Stamp.log"
Say "build output -> $BuildLog"
docker compose build backend *> $BuildLog
$buildRc = $LASTEXITCODE
Say "build exit=$buildRc ; tail of build log:"
if (Test-Path $BuildLog) {
  $tail = Get-Content -Path $BuildLog -Tail 40
  Add-Content -Path $Log -Value $tail -Encoding UTF8
}
if ($buildRc -ne 0) { Fail "build failed (exit $buildRc), full output in $BuildLog" }
Say 'build OK'

Say '=== STEP 3 recreate container ==='
docker rm -f testhub-backend
if ($LASTEXITCODE -ne 0) { Fail 'rm container failed' }
docker compose up -d backend
if ($LASTEXITCODE -ne 0) { Fail 'up -d failed' }
Say 'container recreated, waiting 35s'
Start-Sleep -Seconds 35

Say '=== STEP 4 verify ==='
$code = curl.exe -s -o NUL -w '%{http_code}' http://localhost:8000/api/
Say "GET /api/ -> $code   (expect 404 = healthy, 000 = process dead)"
if ($code -ne '404') { Fail "health check returned $code" }

# 2026-10-15: drf-spectacular generates the whole schema eagerly, so ANY bad
# filterset_fields/filterset_class makes /api/schema/ return 500 (the real bug we hit:
# PageObjectViewSet declared a non-existent 'execution_mode'). Health check alone
# cannot catch it -> check the schema endpoint too.
$schCode = curl.exe -s -o NUL -w '%{http_code}' --max-time 120 http://localhost:8000/api/schema/
Say "GET /api/schema/ -> $schCode   (expect 200; 500 = some ViewSet declares a bad filter field)"
if ($schCode -ne '200') { Fail "Swagger schema returned $schCode" }

Say '--- modao_workflow.py ---'
docker exec testhub-backend md5sum /app/apps/requirement_analysis/modao_workflow.py
docker exec testhub-backend wc -l /app/apps/requirement_analysis/modao_workflow.py
docker exec testhub-backend grep -c vision_scope /app/apps/requirement_analysis/modao_workflow.py

Say '--- modao_extractor.py ---'
docker exec testhub-backend md5sum /app/apps/requirement_analysis/modao_extractor.py
docker exec testhub-backend wc -l /app/apps/requirement_analysis/modao_extractor.py

Say '--- playwright chromium (its install is wrapped in || echo, so failure is SILENT) ---'
# 2026-10-10: must run through sh -c; docker exec does NOT expand globs, so a bare
# `ls -d .../chromium*` always exits 2 (false alarm). Also print the binary version.
docker exec testhub-backend sh -c "ls -d /root/.cache/ms-playwright/chromium*/chrome-linux64 && /root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome --version"
if ($LASTEXITCODE -ne 0) { Fail "chromium MISSING in image (exit $LASTEXITCODE), modao reading will break" }
Say 'chromium check OK'

Say '--- files that were missing on prod ---'
docker exec testhub-backend ls -la /app/apps/ui_automation/element_provider.py
docker exec testhub-backend ls -la /app/apps/ui_automation/element_picker_views.py
docker exec testhub-backend ls -la /app/apps/projects/migrations/0009_projectrole_project_role.py
docker exec testhub-backend ls -la /app/apps/ui_automation/migrations/0015_aiexecutionrecord_replay_degraded.py

$roles = curl.exe -s -o NUL -w '%{http_code}' http://localhost:8000/api/projects/roles/
Say "GET /api/projects/roles/ -> $roles   (expect 401 = route exists, 404 = still old code)"

Say '=== STEP 5 verify prompts + zentao coverage fix (md5 expectation table) ==='
# Expected md5 = host files via: Get-FileHash <file> -Algorithm MD5
# If a host file changes on purpose, update the hash below in the same commit.
$Expect = @{
  '/app/tester.md'                                                        = 'B929122FFFC6E4D224279E2AF087215F';
  '/app/tester_pro.md'                                                    = '147696BB6324666F7005FB50971AB56D';
  '/app/apps/requirement_analysis/independent_skill/prompts/tester.md'     = 'ABFDD8548E5B852C3143F844EED62764';
  '/app/apps/requirement_analysis/independent_skill/prompts/tester_pro.md' = 'FAF9D37D6361CD1A7C1F9089A40A433E';
  '/app/apps/zentao/services.py'                                          = '7FBAD39D20E81331E890FCC63F3A5E61';
  # 2026-11-04: p2 强制 feature_ref/api_ref + p7 强制 ref 并集透传/category 保真（commit 0ae6a19）
  '/app/apps/requirement_analysis/modao_skill/prompts/p2_testpoint_gen.md' = 'B042C48309C0BA93DAB731A0EC097F53';
  '/app/apps/requirement_analysis/modao_skill/prompts/p3_case_gen.md'      = '6596C958C826F7003F7003197AB8B428';
  '/app/apps/requirement_analysis/modao_skill/prompts/p7_merge_dedup.md' = '9EB29E57F4E624BE029EECC35ADBB9C1';
  # 2026-11-05: p0 硬约束 29/30/31（模块=主画布+多状态、状态差异各自成 key_features、画布边界不可越界）
  '/app/apps/requirement_analysis/modao_skill/prompts/p0_requirement_struct.md' = '809B3460D76C2197CC32A6A91F1BABA4';
  # 2026-11-05: _adopted_entry_keys 改两次批量查询 + _mname_cache（实测 1152 次 SQL/3.58s -> 54 次/0.198s）
  '/app/apps/requirement_analysis/modao_views.py'                         = '6D4B6B31B7F069ECDF751034E90A6C27';
  '/app/apps/requirement_analysis/serializers.py'                         = '3FBDA2469C2CB1889D0C8AAECE9ECF94';
  # 2026-11-05: 阶段边界/逐画布循环 close_old_connections + _page_has_requirement_annotation
  '/app/apps/requirement_analysis/modao_workflow.py'                      = 'F407DB9F17BA551B31DAF48AEA35AA41';
  '/app/apps/requirement_analysis/ai_model_service.py'                    = '7D62F51CBF119B2AE44E99467FA572F8';
  # 2026-10-15: PageObjectViewSet.filterset_fields 去掉不存在的 execution_mode（Swagger /api/schema/ 500）
  '/app/apps/ui_automation/views.py'                                      = '61C714CE1058F16EADF4ACA05A900AD2';
  # 2026-11-05: 用例生成/重新评审/用例审计 4 处 threading.Thread 套 _thread_with_conn_cleanup
  #             + save_stream_buffer 加 finally close_old_connections（MySQL 1040 Too many connections）
  '/app/apps/requirement_analysis/views.py'                               = '211BE5170CA7F1B10923F9064E95D507'
}
$md5Raw = docker exec testhub-backend md5sum /app/tester.md /app/tester_pro.md /app/apps/requirement_analysis/independent_skill/prompts/tester.md /app/apps/requirement_analysis/independent_skill/prompts/tester_pro.md /app/apps/zentao/services.py /app/apps/requirement_analysis/modao_skill/prompts/p0_requirement_struct.md /app/apps/requirement_analysis/modao_skill/prompts/p2_testpoint_gen.md /app/apps/requirement_analysis/modao_skill/prompts/p3_case_gen.md /app/apps/requirement_analysis/modao_skill/prompts/p7_merge_dedup.md /app/apps/requirement_analysis/modao_views.py /app/apps/requirement_analysis/serializers.py /app/apps/requirement_analysis/modao_workflow.py /app/apps/requirement_analysis/ai_model_service.py /app/apps/ui_automation/views.py /app/apps/requirement_analysis/views.py
$md5Bad = 0
foreach ($line in $md5Raw) {
  $parts = ($line -split '\s+') | Where-Object { $_ -ne '' }
  if ($parts.Count -lt 2) { continue }
  $h = $parts[0].ToUpper(); $f = $parts[1]
  if (-not $Expect.ContainsKey($f)) { Say "MD5 UNKNOWN-PATH $f = $h"; continue }
  if ($Expect[$f] -eq $h) { Say "MD5 OK   $f = $h" }
  else { Say "MD5 MISMATCH $f : image=$h expected=$($Expect[$f])"; $md5Bad++ }
}
if ($md5Bad -gt 0) { Fail "$md5Bad file(s) did NOT make it into the image" }

Say '--- 2026-10-14 晚 anchors (self-heal loop + manual source) ---'
# md5 only proves the file was copied; these prove the new code paths are really there.
# (no spaces in the patterns, so PS does not mangle the docker exec argv)
docker exec testhub-backend grep -c max_redundant /app/apps/requirement_analysis/ai_model_service.py
docker exec testhub-backend grep -c source=source /app/apps/requirement_analysis/modao_views.py
Say '--- 2026-10-15 anchor (p0 底模降噪条款在位；ui_automation 由 STEP4 的 /api/schema/ 200 覆盖) ---'
# NOTE: do NOT use `grep -c execution_mode` here -- that string legitimately appears
# (serializer fields etc.). The real regression guard is the STEP4 schema check.
docker exec testhub-backend grep -c ui_screen /app/apps/requirement_analysis/modao_skill/prompts/p0_requirement_struct.md
Say '--- 2026-11-05 anchors (connection cleanup really present in the image) ---'
# Expect >=1 each; 0 means the fix was docker-cp'ed but never baked into the image.
docker exec testhub-backend grep -c _thread_with_conn_cleanup /app/apps/requirement_analysis/views.py
docker exec testhub-backend grep -c _mname_cache /app/apps/requirement_analysis/modao_views.py
# 2026-10-14: was hardcoded to 'all 5 md5 match' and silently went stale when two
# modao prompts were added. Derive the count from the table so it cannot drift.
Say "all $($Expect.Count) md5 match -> prompts + zentao/services.py are baked into the image"

Say '--- runtime constants loaded by the rebuilt image (expect 600 / 600 / 0.0) ---'
docker exec testhub-backend python -c "import os,sys;sys.path.insert(0,'/app');os.environ.setdefault('DJANGO_SETTINGS_MODULE','backend.settings');import django;django.setup();from apps.zentao import services as s;print('JUDGE_CASE_CHARS=%s'%s.JUDGE_CASE_CHARS);print('JUDGE_BUG_CHARS=%s'%s.JUDGE_BUG_CHARS);print('JUDGE_TEMPERATURE=%s'%s.JUDGE_TEMPERATURE)"
if ($LASTEXITCODE -ne 0) { Fail 'failed to read JUDGE_* constants from apps.zentao.services' }

Say '--- PromptConfig lengths (expect writer 3089 / reviewer 1012 / analyzer 1466-custom) ---'
docker exec testhub-backend python -c "import os,sys;sys.path.insert(0,'/app');os.environ.setdefault('DJANGO_SETTINGS_MODULE','backend.settings');import django;django.setup();from apps.requirement_analysis.models import PromptConfig;[print('%s len=%d'%(p.prompt_type,len(p.content or ''))) for p in PromptConfig.objects.filter(is_active=True).order_by('prompt_type')]"

Say '--- cross-version knowledge entries (expect >= 16 after report#29 promote) ---'
docker exec testhub-backend python -c "import os,sys;sys.path.insert(0,'/app');os.environ.setdefault('DJANGO_SETTINGS_MODULE','backend.settings');import django;django.setup();from apps.projects.models import ProjectKnowledge;print('global=%d'%ProjectKnowledge.objects.filter(version__isnull=True).count());print('confirmed=%d'%ProjectKnowledge.objects.filter(needs_review=False).count())"

Say '=== STEP 6 modao p2/p3 prompt runtime check ==='
# The modao prompts live in files (NOT in PromptConfig) and are cached in a module-level
# _CACHE inside modao_prompts.py: get_prompt() reads disk ONCE, then never again.
# So verifying the file exists is NOT enough - we must call get_prompt() and check the
# anchors are actually visible to the running process.
# The script writes its own report to /tmp and prints nothing to stdout, because
# `docker exec python -c` output does not land in this log file.
docker exec testhub-backend python /app/scripts/_verify_modao_prompts.py
if ($LASTEXITCODE -ne 0) { Fail 'modao prompt verify script failed (is scripts/ in the image?)' }
$vp = docker exec testhub-backend cat /tmp/_verify_modao_prompts.txt
Add-Content -Path $Log -Value $vp -Encoding UTF8
Write-Output $vp
if (($vp -join "`n") -notmatch 'RESULT=SYNCED') { Fail 'modao p2/p3 prompts NOT synced in the rebuilt image' }
Say 'modao p2/p3 prompts verified in the running container'

Say '=== DONE ==='
Say "log file = $Log"
