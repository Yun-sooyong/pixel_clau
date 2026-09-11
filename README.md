# Claude Float

Windows 화면 맨 앞에 떠 있는 픽셀 클로드 버튼. [Claude Code](https://claude.com/claude-code) CLI 세션에 바로 질문합니다.

| 동작 | 결과 |
|---|---|
| 클릭 | 도트 입력창 (Enter 전송, Esc 닫기) — 클로드가 입력창 쪽을 보고, 타이핑에 맞춰 통통 튀고, 전송하면 냠냠 먹음 |
| 더블 클릭 | 말풍선 채팅창 열기 / 다시 더블 클릭으로 닫기 |
| 드래그 | 이동 (위치 기억) |
| 우클릭 | 세션 선택 · 권한 모드 · 새 세션 · 종료 |

상태 표시: 처리중 = 총총 걷기 + 주황 배지, 완료 = `^ ^` 폴짝 + 초록 배지 + 알림음, 오류 = 빨강 배지.

## 요구 사항
- Windows, Python 3.8+ (표준 라이브러리만 사용)
- `claude` CLI 설치 및 로그인 (`claude` 실행 후 `/login`)

## 실행
```
pythonw claude_float.pyw
```
시작 프로그램 등록: `shell:startup` 폴더에 위 명령의 바로가기를 넣으면 됩니다.

## 동작 방식
선택한 세션의 작업 폴더에서 `claude -p --resume <세션ID> --output-format stream-json`을 실행하고, 프롬프트는 stdin으로 넘깁니다.
세션 목록과 대화 기록은 `~/.claude/projects/*/*.jsonl`에서 읽습니다 (데스크톱 앱 세션 포함).
설정(선택 세션, 위치, 권한 모드)은 스크립트 옆 `claude_float.json`에 저장됩니다.
