# herdr-trace 공개 계획

> **herdr-trace** — see what passed between your coding agents in herdr, and whether it arrived.
>
> herdr 안에서 여러 도구(Codex, Claude Code)의 에이전트가 서로 무엇을 주고받았고 그것이 실제로 도착했는지를,
> 근거와 함께 그래프와 시간 순으로 보여 주는 로컬 모니터.

작성: 2026-10-10. 출발점은 실험 저장소 `~/003.Lab/experiments/codex-dag-herdr`(이하 "실험 저장소")다.
실험 저장소는 Codex DAG(`~/002.Business/github/codex-dag`)를 통째로 가져와 그 위에 herdr 관찰 기능을 얹었다.
herdr-trace는 그중 herdr 관찰 기능만 떼어 낸 별도 프로젝트다. 원본 `codex-dag`는 고치지 않는다.

## 1. 결정 사항 (2026-10-10)

| 항목 | 결정 |
| --- | --- |
| 이름 | `herdr-trace`. CLI 명령도 `herdr-trace` |
| 저장소 | `~/002.Business/github/herdr-trace` (이 저장소). 원격 연결과 공개는 준비가 끝난 뒤 사용자가 요청할 때 한다 |
| 형태 | 독립 실행 코어 + 얇은 herdr 플러그인 (2장) |
| 화면 언어 | 영어 기본, 한국어 선택 |
| 지원 범위 | macOS, Linux. Python 3.11 이상. Windows는 지원하지 않는다(herdr 소켓 방식이 다르다) |
| herdr 시작 시 | 플러그인 시작 훅이 모니터 서버만 띄운다. 화면은 herdr 액션이나 주소로 연다. 브라우저 자동 열기는 설정으로 켠다(기본 끔) |
| 라이선스 | MIT. `codex-dag`(MIT, © codesholic)에서 가져온 코드라는 출처를 표기한다 |

**버린 이름과 이유**
- `herdr-dag-with-webui`: herdr web ui(devswha의 플러그인)를 품고 있다고 오해할 수 있다. 실제로는 우리 화면이 따로 있고, web ui는 있으면 연결되는 선택 기능이다.
- `herdr-dag` 계열: `jc01rho/omo-herdr-dag`(OmO 작업 DAG를 herdr pane에 보여 주는 확장)와 헷갈린다.
- `receipts` 계열: `alepar/herdr-threads`(Claude Code·Codex 에이전트 사이의 스레드와 수신 확인)와 개념이 겹친다.
- 확인한 시점(2026-10-10)에 `herdr-trace`는 GitHub 저장소 이름과 PyPI 패키지 이름이 비어 있었다. 상표 검색은 하지 않았다.

## 2. 다른 도구와의 관계

| 도구 | 하는 일 | herdr-trace와의 차이 |
| --- | --- | --- |
| herdr-radar, herdr-lantern, herdr-projects | 누가 일하는 중이고 누가 응답을 기다리는지 보여 주는 개요 | herdr-trace는 에이전트 **사이에** 오간 것과 도착 여부를 근거와 함께 보여 준다 |
| herdr web ui | 브라우저에서 pane 대화·터미널·승인 조작 | herdr-trace는 읽기 전용이다. web ui 주소를 넣으면 카드에서 그 pane을 web ui로 연다 |
| omo-herdr-dag | OmO 한 세션의 작업 DAG를 pane에 표시 | herdr-trace는 도구를 가리지 않고 여러 에이전트 사이의 흐름을 본다 |
| herdr-threads | 에이전트 그룹 스레드와 수신 확인(전송 쪽) | herdr-trace는 보내는 래퍼도 있지만 중심은 관찰과 근거 판정이다 |

소개 문구 방향: "radar와 lantern은 누가 당신을 기다리는지 보여 준다. herdr-trace는 에이전트 사이에 무엇이 오갔고 도착했는지를 보여 준다."

## 3. 구조

**코어 (Python 표준 라이브러리만)**

| 명령 | 하는 일 | 실험 저장소의 출처 |
| --- | --- | --- |
| `herdr-trace serve` | 로컬 웹 서버(127.0.0.1 전용, 토큰 인증). 그래프·시간 순 화면, SSE 실시간 갱신 | `monitor.py`의 서버 부분, `lab_view.py`, `lab.html` |
| `herdr-trace send` | 에이전트가 다른 에이전트에게 보내는 래퍼. 원문을 저널에 먼저 남기고 herdr로 보낸다 | `scripts/codex-dag-send`, `herdr_bridge.py` |
| `herdr-trace rules` | 에이전트가 따를 협업 규칙(AGENTS.md에 넣을 글)을 출력한다 | 샌드박스 `AGENTS.md` |
| `herdr-trace demo` | 꾸며낸 데이터와 가짜 herdr 소켓으로 화면을 띄운다(README 화면, 시험용) | `scripts/lab-demo.py` |

설치: `pipx install herdr-trace` 또는 `uvx herdr-trace`, 아니면 저장소를 받아 바로 실행.
래퍼는 에이전트가 PATH에서 바로 부를 수 있어야 한다. 플러그인 폴더 경로는 일정하지 않아서 에이전트 규칙에 적기 어렵다.

**herdr 플러그인 (이 저장소의 하위 폴더, 예: `herdr-plugin/`)**

- `herdr plugin install <owner>/herdr-trace/herdr-plugin`으로 설치한다. 개발 중에는 `herdr plugin link`.
- **시작 훅:** herdr가 넘겨주는 그 세션의 소켓(`HERDR_SOCKET_PATH`)과 세션 이름으로 `herdr-trace serve`를 띄운다.
  - 시작 훅은 한 번 실행될 뿐 herdr가 프로세스를 지켜 주지 않는다. 그래서 web ui처럼 서버를 떼어 내고 pid를 기록한다. 이미 떠 있으면 다시 띄우지 않는다.
  - 훅은 사용자 셸이 아니라 herdr의 환경에서 돈다. `python3`와 `herdr-trace`를 찾을 경로를 보강해야 한다.
- **액션:** "모니터 열기"(토큰을 넘겨 브라우저로 연다), "상태", "끄기".
- **pane(선택):** 주소와 상태를 글자로 보여 준다. 그래프는 브라우저에서만 본다.

**데이터 흐름**

```
에이전트 ──herdr-trace send──▶ 저널(원문 먼저) ──▶ herdr agent prompt ──▶ 받는 에이전트
                                  │
herdr-trace serve ◀── 저널 + 받는 쪽 기록 파일(~/.codex, ~/.claude, 읽기만) + herdr 소켓(읽기 요청만)
```

## 4. 가져올 것과 버릴 것

**가져올 것** (실험 저장소 기준 줄 수)

| 파일 | 줄 | 비고 |
| --- | --- | --- |
| `src/monitor/lab_view.py` | 437 | 묶음 생성, 소스 설정, 계보, 중간 응답 판정, web ui 링크 |
| `src/monitor/lab.html` | 1125 | 그래프·시간 순 화면. 문구를 영어/한국어 사전으로 분리해야 한다 |
| `src/monitor/delivery.py` | 188 | 전달 판정, 늦게 보고된 받는 쪽 세션 찾기, 도출 결과 |
| `src/monitor/claude_source.py` | 271 | Claude Code 기록 읽기(증분), 서브에이전트, 실행 중 세션 |
| `src/monitor/codex_source.py` | 98 | Codex rollout 읽기(증분) |
| `src/monitor/herdr_source.py` | 163 | herdr 소켓 읽기 전용 클라이언트, 이벤트 구독 |
| `src/monitor/herdr_bridge.py` | 203 | 래퍼 본체, 보낸 쪽 확인 |
| 테스트 | 약 1,500 | 꾸며낸 데이터 픽스처(`lab_fixtures.py`)와 7개 테스트 파일 |

**대신 만들 것** (실험 코드가 원본 Codex DAG에 기대던 부분)
- **저널:** 래퍼가 `monitor.append_event`(원본 `journal.py` 336줄의 공유 저널 체계)로 쓴다. herdr-trace에는 세션별 이벤트 파일 하나에 덧붙이기만 하는 작은 저널이면 된다. 잠금과 순번은 유지한다.
- **서버:** 원본 `monitor.py`(819줄)에서 필요한 부분만 옮긴다.
  - 토큰 인증(주소의 `?token=`으로 시작해 쿠키로 바꾸기), Host 헤더 확인, CSP, SSE.
  - 원본 서버에는 인증과 Host 확인이 이미 있다. 실험에서는 `--dev-no-auth`로 인증을 꺼 두고 썼다.

**버릴 것**
- Codex DAG의 수집기(`collector.py` 1,480줄), 원래 화면(`index.html`)과 그 위의 "협업 묶음" 오버레이, 메뉴바·트레이 앱, 설치 파일 빌드.

## 5. 공개 전에 반드시 고칠 것

1. **인증을 켠 채로 띄운다.** 실험 서버는 `--dev-no-auth`였다. 기록 파일 원문을 보여 주는 서버이므로 토큰 인증과 Host 확인을 끌 수 없게 한다. 플러그인의 "모니터 열기"가 토큰을 넘긴다.
2. **화면 문구를 영어 기본, 한국어 선택으로 나눈다.** 문구가 지금은 코드 곳곳에 한국어로 박혀 있다.
3. **실험 전용 가정을 걷어 낸다.**
   - `dag-lab` 세션, 포트 58977, `.local/lab-*` 데이터 폴더를 없앤다.
   - 데이터는 세션별로 사용자 상태 폴더(XDG state)에 둔다.
   - 세션이 여러 개일 때 포트를 정하는 규칙을 둔다.
   - "기본 세션은 건드리지 않는다"는 실험 규칙은 "자기를 띄운 세션만 읽는다"로 바뀐다.
4. **래퍼를 쉽게 쓰게 한다.**
   - `rules`가 AGENTS.md용 규칙을 준다.
   - Codex는 샌드박스가 herdr 소켓 접근을 막아서 보낼 때마다 승인이 필요했다. 래퍼만 허용하는 설정이 가능한지 조사해 문서로 안내한다.
5. **상표:** 이름과 화면에 Claude, Codex를 제품 이름처럼 쓰지 않는다. 비공식 도구라는 고지를 넣는다.
6. **Linux 확인과 Python 3.11:** Linux에서 실제로 돌려 보고, 3.12 이상 문법을 쓰지 않았는지 확인한다.
7. **기록 형식이 바뀔 때의 안내:** Codex·Claude 기록과 herdr API는 비공식이고 자주 바뀐다. 모르는 형식을 만나면 화면에 버전과 함께 알린다.
8. **개인정보 원칙을 README에 적는다.** 로컬 전용, 읽기 전용, 외부로 보내는 것 없음, 비공개 추론 수집 안 함.

## 6. 알려진 한계 (실측에서 확인)

- **herdr의 Codex 대화상자 오판정:** herdr가 Codex 시작 대화상자를 `idle`로 판정했다(두 번 재현). 새로 띄운 Codex에게 보내기 전 화면 확인이 필요하다.
- **도출 결과는 추정이다.** 기본 결과 경로는 명시 결과(`--kind result --reply-to`)다. 맡긴 일이 돌아오기 전에 끝난 턴의 응답은 "중간 응답"으로 표시한다.
- **web ui 연결의 한계:**
  - web ui는 자기를 처음 띄운 herdr 세션 하나에만 붙는다. pane ID는 세션마다 겹친다.
  - 링크는 이 화면이 연 web ui 탭만 다시 쓰고, 열 때마다 그 탭을 새로 읽는다.
- **보낸 쪽 확인 방식:** pane 환경변수, 도구 세션 ID, 프로세스 계보 순으로 확인한다. 모두 실패하면 "미검증"으로 남긴다.

## 7. 단계

| 단계 | 내용 | 끝났다는 기준 |
| --- | --- | --- |
| 1. 코어 추출 | 4장의 파일을 옮기고 저널·서버를 새로 만든다. 인증 기본 켜짐. `serve`/`send`/`rules`/`demo` | 옮긴 테스트가 모두 통과한다. `demo`가 인증을 켠 채로 뜬다 |
| 2. 언어 | 문구 사전 분리, 영어 기본, 한국어 선택 | 두 언어로 화면을 확인한다 |
| 3. 플러그인 | `herdr-plugin.toml`, 시작 훅, 액션. `herdr plugin link`로 이 Mac에서 써 본다 | 세션을 다시 시작하면 서버가 뜨고, 액션으로 화면이 열린다 |
| 4. Linux | Linux에서 코어와 플러그인을 확인한다 | 테스트 통과, 플러그인 동작 |
| 5. 공개 알파 | README(데모 화면, 한계, 개인정보), 원격 저장소, 태그 | 사용자 승인 뒤 공개 |

## 8. 업스트림 요청 후보 (선택)

- herdr web ui: health 응답에 세션 이름 넣기, 다른 창에서도 pane 선택 메시지(`select-pane`) 받기.
- herdr: `agent prompt`의 보낸 쪽을 herdr가 직접 기록하기(장기적으로 래퍼가 필요 없어진다), Codex 대화상자 오판정 보고.

## 9. 열린 질문

- GitHub 계정과 플러그인 ID(`<owner>.herdr-trace`). PyPI에도 올릴지.
- 세션이 여러 개일 때 서버를 세션마다 하나씩 띄울지, 하나가 여러 세션을 볼지.
- 실험 저장소의 저널 형식과 호환을 유지할지(실측 기록을 새 화면에서 다시 볼 수 있게).

## 10. 진행 기록

### 1단계: 코어 추출 (2026-10-10, 완료)

출처는 실험 저장소 `codex-dag-herdr`의 `b9edcae`다. 9장의 저널 호환 질문은 "호환 유지"로 정했다.
이벤트 형식(`agent.message`, `agent.delivery`)을 그대로 두고, `serve --journal`로 실험 저널을 지정해 다시 볼 수 있다.

**만든 것**

| 파일 | 내용 |
| --- | --- |
| `src/herdr_trace/journal.py` | 덧붙이기만 하는 저널. 순번 확인, 옆 파일 잠금, 파일 권한 0600, 이벤트마다 ID |
| `src/herdr_trace/server.py` | 127.0.0.1 서버. 토큰 필수(끄는 옵션 없음), Host·Origin 확인, CSP(`frame-ancestors 'none'`), SSE |
| `src/herdr_trace/paths.py` | 세션별 상태 폴더(`$XDG_STATE_HOME/herdr-trace/sessions/<세션>/`), herdr 소켓, Codex·Claude 폴더 |
| `src/herdr_trace/cli.py` | `serve`, `send`, `rules`, `demo` |
| `src/herdr_trace/demo.py`, `demo_data.py` | 꾸며낸 데이터와 가짜 herdr 소켓 |
| `src/herdr_trace/rules.md` | 에이전트용 협업 규칙(영어) |

**옮기면서 바꾼 것**

- 머리말을 `[herdr-trace msg=…]`로 바꿨다. 실험 저널을 위해 `[codex-dag msg=…]`도 계속 읽는다.
- 설정 파일 이름을 `settings.json`으로 바꿨다. 세션 폴더에 둔다.
- 서버 주소는 `/api/bundle`, `/api/stream`, `/api/settings`다. 기존 화면 오버레이용 임베드 모드는 뺐다.
- 화면 제목에 세션 이름을 보인다. 화면 문구는 아직 한국어다(2단계).

**확인**

- 테스트 74개가 Python 3.13과 3.12에서 통과했다. Python 3.11은 이 Mac에 없어 확인하지 못했다.
  - 실험 저장소의 테스트 7개 파일을 옮겼다.
  - 새로 넣은 것: 서버 접근(토큰, 쿠키, Host·Origin, 세션당 서버 하나, 기록 파일 권한), CLI(규칙, 보내기 거부, 데모), 경로.
- 데모를 브라우저(Chromium)로 확인했다.
  - 토큰 없이 열면 401과 안내가 나온다.
  - 토큰 주소로 열면 HttpOnly 쿠키로 바뀌고 주소에서 토큰이 사라진다.
  - 실시간 갱신이 되고 브라우저 오류는 없다.

**사고와 재발 방지**

- 처음 테스트를 돌릴 때 `herdr 밖에서는 보내지 않는다` 테스트가 실제 herdr 세션(`dag-lab`)의 reviewer에게 메시지 `hi`를 보냈다(메시지 `707912016607`).
  - 이 작업 세션이 herdr pane 안에서 돌고 있었고, 테스트 도우미가 그 환경변수를 그대로 물려줬다.
  - reviewer는 짧은 턴 하나로 "맡긴 일이 아니어서 답하지 않았다"고만 남겼다. 파일 변경이나 다른 전송은 없었다.
- 재발 방지:
  - 테스트 도우미는 `HERDR_*`와 에이전트 세션 변수를 지우고, herdr 실행 파일을 없는 경로로 고정한다.
  - `scripts/test.sh`는 실행 전에 `HERDR_*`를 지운다.
  - 고친 뒤 다시 돌린 테스트에서는 herdr로 나간 요청이 없었다.

### 2단계: 언어 (2026-10-10, 완료)

**서버는 코드와 구조만 보내고, 문장은 화면이 고른 언어로 만든다.**
- 상태는 코드로 보낸다. 예: 전달 상태 `confirmed`, herdr 판정 `idle`·`working`·`blocked`.
- 판정 사유도 코드다. 예: `no_header`, `other_recipient`, `late_recipient_not_found`. 본문 변형 단계(`trailing_spaces` 등)도 같다.
- 턴 제목은 `{text, from, kind}` 또는 `{text, unverified}` 구조로 보낸다. 서버가 한국어로 "lead · 작업 요청 · …"을 만들던 방식을 바꿨다.
- 드물게 보이는 진단 문구(소켓 연결 실패, 기록을 못 읽음, 래퍼의 보낸 쪽 확인 설명)는 영어 한 가지로 둔다.

**화면**
- 문구 약 210개를 `STRINGS` 한 줄(영어/한국어)에 모았다. 정적 문구는 `data-i18n` 속성, 스크립트 문구는 `t("키", {값})`로 바꿨다.
- 기본은 영어다. 위쪽 버튼으로 바꾸고, 고른 언어는 브라우저에 기억한다.
- `serve --lang ko`(데모도 `--lang`)는 아직 고른 적 없는 사람의 시작 언어를 정한다. 고른 사람에게는 그 선택이 서버 기본값보다 우선한다.
- 데이터(에이전트 이름, 메시지 원문, 경로)는 기록된 그대로 보인다. 그래서 꾸며낸 데모 데이터의 한국어 메시지는 영어 화면에서도 한국어로 보인다. 공개용 데모를 영어 데이터로 바꿀지는 5단계(README)에서 정한다.
- 에이전트용 규칙(`rules`)은 영어만 있다.

**확인**
- 테스트 79개가 Python 3.13과 3.12에서 통과했다. 새로 넣은 테스트는 아래와 같다.
  - 화면 파일에서 사전 밖에 한국어가 없다.
  - 영어·한국어 사전의 키가 같다.
  - 페이지와 서버가 쓰는 키가 모두 사전에 있다.
  - `--lang`이 설정 응답에 반영된다.
- 브라우저 검사(Chromium, 데모):
  - 카드, 선, 패널, 시간 순 행을 모두 열어 봤다. 영어 화면에 데이터에 없는 한국어가 없고, 두 언어 모두 번역 키가 새지 않았다.
  - 언어 전환, 새로 고침 뒤 유지, 서버 기본값과 사용자 선택의 우선순위를 확인했다. 브라우저 오류는 없다.
  - 검사 중에 결함 하나를 찾아 고쳤다. 언어를 바꾸면 설정을 다시 읽으면서 서버 기본값으로 되돌아가던 문제다.

### 3단계: herdr 플러그인 (2026-10-10, 완료)

**만든 것**
- 저장소 루트에 `herdr-plugin.toml`을 뒀다(ID `codesholic.herdr-trace`). 저장소를 받으면 코드까지 같이 있으므로, 플러그인은 `src/`를 그대로 실행한다.
  - 빌드: Python 3.11 이상이 있는지 확인한다.
  - 시작 훅: `herdr-trace plugin start`.
  - 액션: `herdr-trace-open`, `herdr-trace-status`, `herdr-trace-stop`.
- `scripts/herdr-plugin.sh`: herdr의 환경에서 Python을 찾는다(PATH, Homebrew, 시스템 순서, `HERDR_TRACE_PYTHON`으로 지정 가능).
- `src/herdr_trace/plugin.py`:
  - `start`는 서버를 세션별로 떼어 내 띄우고, 이미 떠 있으면 아무것도 하지 않는다.
  - herdr가 넘겨주는 `HERDR_SESSION`과 `HERDR_SOCKET_PATH`로 세션과 소켓을 정한다. 상태 폴더는 `send`와 같은 곳이라 같은 저널을 쓴다.
  - 플러그인 출력(herdr 로그에 남는다)에는 주소만 쓰고 토큰은 쓰지 않는다. 토큰은 브라우저에만 넘기고, 서버 로그(`server.log`, 권한 0600)에 남는다.
  - 설정은 환경변수나 플러그인 설정 폴더의 `env` 파일로 준다. `HERDR_TRACE_AUTO_OPEN=1`(시작할 때 브라우저 열기, 기본 끔), `HERDR_TRACE_LANG=ko`.

**확인: 격리된 herdr 0.9.3**
- `XDG_CONFIG_HOME` 등을 임시 폴더로 바꾸고 빈 환경으로 화면 없는 서버(`herdr --session trace-test server`)를 띄웠다.
  - herdr는 `XDG_CONFIG_HOME`을 따랐다. 소켓, 플러그인 등록부, 플러그인 상태가 모두 임시 폴더에 생겼다.
  - 사용자 herdr(`~/.config/herdr`)와 `dag-lab` 세션에는 등록하거나 다시 시작하지 않았다.
- `herdr plugin link`로 연결했다.
  - 연결만으로는 시작 훅이 돌지 않았다. 시험용 서버를 다시 시작하자 시작 훅이 성공했다(0.3초).
  - 시작 훅은 세션 `trace-test`와 그 소켓을 받아 herdr-trace를 띄웠고, 실제 herdr에 연결해 이벤트를 구독했다.
- 액션(`plugin action invoke`)으로 다음을 확인했다.
  - 상태: 서비스 중을 보고했다.
  - 열기: 토큰 주소는 브라우저 대신 쓴 기록용 스크립트에만 갔다.
  - 끄기: 프로세스가 사라졌다.
  - 끈 뒤 열기: 서버를 새로 띄우고 열었다.
- herdr 플러그인 로그에 토큰이 한 번도 나오지 않았다.
- 시험을 마치고 시험용 herdr와 임시 폴더를 지웠다.
  - 사용자 herdr 설정을 시작 전 기록과 대조했다. `dag-lab` 자신의 기록(세션 상태 저장, 그 시각에 붙었다 떨어진 herdr 클라이언트 로그)만 바뀌었다.
  - 시험 흔적(`trace-test`, `/tmp/htx`)은 없었다.

**herdr 0.9.3에서 알게 된 것**
- 액션 실행은 비동기다. 결과는 플러그인 로그로 본다(`herdr plugin log list`).
- `plugin action invoke --plugin <ID>`는 어떤 형식으로도 동작하지 않았다(`plugin_action_not_found`). 액션은 ID만으로 실행된다.
  - web ui도 `status`, `stop`을 갖고 있어서 이름이 겹친다. 그래서 액션 ID를 `herdr-trace-…`로 지었다.
- `plugin link`는 빌드 단계를 돌리지 않았다. GitHub 설치(`plugin install`)에서 도는지는 저장소 공개 전이라 확인하지 못했다.

**남은 것**
- 에이전트가 보내려면 `herdr-trace` 명령이 PATH에 있어야 한다. 플러그인만으로는 생기지 않는다. 설치 안내(pipx 등)나 명령을 만들어 주는 액션은 4·5단계에서 정한다.
- 세션이 여러 개면 세션마다 서버가 하나씩, 빈 포트로 뜬다(9장 질문의 답).
