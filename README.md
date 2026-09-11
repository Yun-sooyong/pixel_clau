# Claude Float

Windows 화면 맨 앞에 떠 있는 픽셀 클로드. 클릭 한 번으로 [Claude Code](https://claude.com/claude-code)에 질문합니다.

## 설치 (사용자)

1. **Claude Code 설치 + 로그인** — 터미널에서 `claude`를 한 번 실행해 로그인해 두세요. (필수)
2. [Releases](../../releases/latest)에서 **`ClaudeFloat.exe`** 다운로드 → 원하는 폴더에 두고 실행.
   - 파이썬 설치는 필요 없습니다.
   - 서명되지 않은 exe라 처음 실행 시 "Windows의 PC 보호" 창이 뜰 수 있어요 → **추가 정보 → 실행**.
3. 오른쪽 클릭 → **윈도우 시작 시 자동 실행**을 켜면 로그인할 때마다 뜹니다.

## 사용법

| 동작 | 결과 |
|---|---|
| 클릭 | 도트 입력창 (Enter 전송 · 다시 클릭/Esc 닫기). 클로드가 입력창을 쳐다보고, 타이핑에 맞춰 통통 튀고, 전송하면 냠냠 먹어요 |
| 더블 클릭 | 말풍선 채팅창 열기 / 다시 더블 클릭으로 닫기 |
| 드래그 | 이동 (열린 창도 같이 따라옴, 채팅창을 끌어도 클로드가 따라옴) |
| 오른쪽 클릭 | 세션 선택 · 새 세션 · 자동 실행 · 종료 |

상태 표시: 처리중 = 총총 걷기 + 주황 배지 · 완료 = `^ ^` 폴짝 + 초록 배지 + 알림음 · 오류 = 빨강 배지

**세션 선택**: 폴더별로 묶인 목록에서 검색해서 고릅니다. 데스크톱 앱 세션은 앱 사이드바와 같은 제목으로 보여요.

**데스크톱 앱 세션에 질문하면**: 앱 밖에서는 앱에 열린 대화에 메시지를 넣을 수 없어서,
질문을 클립보드에 복사하고 앱에서 그 세션을 열어 줍니다 → 앱 입력창에서 **Ctrl+V, Enter**.
채팅창은 앱의 대화를 실시간으로 따라가고, 클로드는 앱의 처리중/완료를 표시합니다.
터미널에서 만든(CLI) 세션은 픽셀 창에서 바로 답을 받습니다.

## 동작 방식

- CLI 세션: 세션 폴더에서 `claude -p --resume <세션ID> --output-format stream-json` 실행 (프롬프트는 stdin)
- 대화 기록: `~/.claude/projects/*/*.jsonl`을 **추가된 부분만** 읽어서 따라감 (17MB 세션도 새로고침 ~5ms)
- 데스크톱 앱 세션 목록: `%APPDATA%\Claude\claude-code-sessions`
- 설정 (선택 세션, 위치, 권한 모드): `%APPDATA%\ClaudeFloat\config.json`
- 자동 실행: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (관리자 권한 불필요)

## 소스로 실행 / 빌드 (개발자)

```
pythonw claude_float.pyw
```
표준 라이브러리만 씁니다 (Python 3.8+, Windows).

exe 빌드:
```
python -m venv .venv-build
.venv-build\Scripts\pip install pyinstaller pillow
.venv-build\Scripts\python build.py
```
→ `dist\ClaudeFloat.exe` (아이콘도 스프라이트 데이터에서 그대로 생성)
