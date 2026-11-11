# HPC Monitor

30분 간격으로 수집한 HPC 로그 6종을 읽어 **노드·partition·사용자별 사용량**을 조회합니다.
HTML 독립 실행판과 같은 집계 기준을 사용하는 Python X11 GUI 및 Python 웹 GUI를 제공합니다.

| 버전 | 실행 파일 | 실행 환경 |
|---|---|---|
| HTML | `HPC_Usage_Monitor.html` | Chrome/Edge에서 파일 열기. 설치·서버 불필요 |
| Python X11 GUI | `HPC_Monitor_X11.py` | Python 3.10+, Tkinter, Matplotlib, X11 또는 데스크톱 |
| Python Web GUI | `HPC_Monitor_Web.py` | Python 3.10+ 서버 + PC 브라우저. X11·Node.js·Flask 불필요 |

Python 파일 두 개는 **공통 집계 모듈을 실제로 사용**합니다. 웹 버전이 기존 HTML을 단순히 제공하는 서버는 아닙니다.
TSV 파싱·작업 중복 제거·시간 적분·필터링을 Python에서 수행하고, 브라우저는 그래프와 표를 표시합니다.
공통 파일과 HTML 자산을 사용하므로 저장소 전체를 다운로드해 같은 폴더에 두세요.

## 설치

```bash
git clone https://github.com/jinstone2001-stack/hpc-monitor.git
cd hpc-monitor
```

웹 GUI만 사용한다면 추가 패키지가 없습니다. X11 GUI를 사용하려면:

```bash
python3 -m pip install -r requirements.txt
python3 -m tkinter
```

두 번째 명령은 Tkinter 설치와 화면 연결을 확인하는 테스트 창을 엽니다.
Ubuntu/Debian에서 Tkinter가 없다면 `sudo apt-get install python3-tk`로 설치할 수 있습니다.
Matplotlib는 pip로 설치하지만 Tkinter 자체는 일반적으로 OS/Python 배포판에서 제공합니다.

## 1. HTML 버전

`HPC_Usage_Monitor.html`을 PC의 Chrome/Edge에서 열고 **로그 불러오기**를 선택하세요.
인터넷 연결 없이 동작하며 선택한 로그를 외부 서버로 전송하지 않습니다.
원래 제작한 HTML 파일을 그대로 포함했습니다.

## 2. X11 GUI 버전

```bash
python3 HPC_Monitor_X11.py
python3 HPC_Monitor_X11.py --demo
python3 HPC_Monitor_X11.py /path/node_cpu.tsv /path/user_cpu.tsv
```

MobaXterm에서는 로컬 X 서버를 켜고 SSH 세션의 **X11 forwarding**을 활성화한 후 서버에서 실행하세요.
일반 SSH라면 로컬 PC의 X 서버가 켜진 상태에서:

```bash
ssh -X -C USER@SERVER
cd /path/to/hpc-monitor
python3 HPC_Monitor_X11.py
```

그래프 선택, 표 검색·정렬·페이지 이동, 행 상세 보기, 그룹·노드·사용자 다중 선택,
기간 설정, 다크 모드, CSV·PNG·통합 JSON 저장을 지원합니다.
Matplotlib 도구 모음으로 확대·이동할 수 있습니다.
원격 X11의 네트워크 지연이 크면 같은 Python 엔진을 쓰는 웹 GUI를 이용하세요.

## 3. Python 웹 GUI 버전

PC에서 직접 실행:

```bash
python3 HPC_Monitor_Web.py --port 8888
```

브라우저에서 `http://127.0.0.1:8888/`를 엽니다.
기본값은 로컬 주소에만 연결되며, 선택한 로그는 실행 중인 Python 서버의 메모리로 전달됩니다.
사용자가 저장하지 않는 한 로그를 서버 디스크에 자동 저장하지 않습니다.

원격 HPC 서버에서 실행:

```bash
python3 HPC_Monitor_Web.py --port 8888 --no-browser
```

로컬 PC의 별도 터미널에서 SSH 터널을 연결합니다.

```bash
ssh -N -L 8888:127.0.0.1:8888 USER@SERVER
```

그 후 **로컬 PC 브라우저**에서 `http://127.0.0.1:8888/`를 엽니다.
서버의 Firefox나 X11 브라우저를 띄울 필요가 없습니다.
SSH로 실행하면 서버 브라우저 자동 실행을 생략합니다.

```bash
python3 HPC_Monitor_Web.py --demo --port 8888 --no-browser
python3 HPC_Monitor_Web.py /path/node_cpu.tsv /path/user_cpu.tsv --port 8888 --no-browser
```

종료는 `Ctrl+C`입니다. 포트가 사용 중이면 다른 `--port`를 지정하세요.
한 번의 업로드 기본 제한은 128 MB이며 `--max-upload-mb 256`처럼 변경할 수 있습니다.
이 서버는 개인 분석용 단일 세션입니다. `--host 0.0.0.0`으로 외부에 열면 접속자들이 같은 로그 세션을 공유하므로,
개인 사용에는 기본 로컬 주소와 SSH 터널을 권장합니다.

## 로그 형식

구분자는 **탭**이고 첫 행에 항목명이 있어야 합니다.
파일명보다 헤더를 우선해 종류를 판별하므로 확장자가 없는 파일도 읽을 수 있습니다.
여섯 종류를 모두 넣을 필요는 없으며, 있는 데이터로 계산할 수 있는 지표만 표시합니다.

| 종류 | 필드 |
|---|---|
| `user_cpu` | timestamp, user, node, partition, cpus_alloc, mem_alloc_mb, n_jobs, job_ids |
| `queue_started` | timestamp, job_id, user, partition, submit_time, start_time, wait_sec, wait_min, alloc_cpus, n_nodes, state |
| `queue_pending` | timestamp, job_id, user, partition, qos, req_cpus, submit_time, wait_sec, wait_min, priority, reason |
| `node_cpu` | timestamp, node, node_type, partitions, state, cpu_total, cpu_alloc, alloc_pct, cpu_load, load_pct, mem_total_mb, mem_alloc_mb, mem_pct |
| `gpu_user` | timestamp, node, gpu_index, user, job_id, pid, mem_used_mb, proc_name |
| `gpu` | timestamp, node, gpu_index, uuid, gpu_name, gpu_util, mem_total_mb, mem_used_mb, mem_pct, temp_c, power_w |

- 시각 예시: `2026-11-10T00:30:00`. 시간대가 없는 로그는 기본 한국시간이며 설정에서 UTC로 변경할 수 있습니다.
- 노드 그룹은 직접 명명한 `partition`/`partitions` 값으로 구성합니다.
- 여러 job ID 또는 partition은 쉼표·공백·세미콜론 등으로 구분하거나 JSON 배열 표기를 사용할 수 있습니다.
- 여러 날짜의 같은 종류 파일을 추가할 수 있으며 같은 식별자·시각은 중복 제거합니다.
- 서로 다른 여섯 헤더를 이어 붙인 TSV 파일도 지원합니다. 각 구간에 헤더가 필요합니다.
- **통합 JSON**으로 저장하면 세 버전 사이에서 다시 불러올 수 있습니다. 예제 여부도 보존합니다.

## 집계 기준

| 지표 | 정의 |
|---|---|
| 제출 작업 수 | 관측된 고유 작업 중 submit_time이 조회 구간 안에 있는 작업 |
| 시작 작업 수 | 관측된 고유 작업 중 start_time이 조회 구간 안에 있는 작업 |
| 실행 작업 수 | 실행 구간이 조회 구간과 겹치는 고유 작업. 다중 노드 작업은 전체에서 한 번 |
| 누적 작업 시간 | 작업별 실행 구간을 합산. 동시 작업은 각각 합산 |
| 코어 시간 | 할당 코어 수 × 관측 간격, 단위 core·h |
| 작업 점유율 | 작업이 하나 이상 실행된 시간 ÷ 기준 시간. 중복 실행 시간은 합집합 |
| 코어 할당률 | 할당 코어 시간 ÷ 전체 코어 시간 |
| GPU 사용자 점유 | 같은 사용자·GPU의 프로세스 관측 구간 합집합, 단위 GPU·h |
| 관측률 | 확인된 노드 기록 시간 ÷ 선택 기간의 전체 노드 시간 |

조회 구간은 **시작 이상·종료 미만**입니다. 현황 상태는 종료 시각의 스냅샷까지 확인합니다.
자정을 넘는 작업은 날짜별로 나누며 그룹 사용률은 노드·코어 용량으로 가중 집계합니다.
사용자 기여율의 분모는 해당 사용자 사용량이 아니라 선택 노드 전체 코어 용량입니다.
공통 노드가 있는 partition별 결과는 서로 중복될 수 있지만 전체 합계는 노드를 한 번만 셉니다.

**시간·종료는 추정입니다.** 상태를 다음 기록까지 유지하고, 다음 확인 시각에 사라진 작업은 종료로 추정합니다.
긴 로그 공백은 최대 한 주기까지만 적분하고 마지막 기록 이후는 연장하지 않습니다.
소멸은 정상 완료를 뜻하지 않으므로 성공·실패 건수로 변환하지 않습니다.
30분 사이에 시작하고 끝나 기록에 한 번도 잡히지 않은 작업은 복원할 수 없습니다.
기본 사용률은 관측시간 기준입니다. 설정에서 전체 조회기간 기준의 하한값도 선택할 수 있습니다.

`node_cpu` 시각과 사용자·대기 기록을 연동하는 옵션은 **동일 수집 주기의 전체 스냅샷**을 전제로 합니다.
빈 스냅샷과 수집 실패는 완전히 구분할 수 없습니다. 파일 일부가 빠졌다면 옵션과 관측 범위를 확인하세요.
`queue_started`가 새로 시작한 작업만 추가하는 로그라면 설정에서 **시작 이벤트** 모드로 바꾸세요.
이 모드에서는 `user_cpu` 등 별도 실행 관측이 없는 작업의 실행 시간을 만들어내지 않습니다.

코어 할당률·CPU load를 실제 CPU 계산 효율로 해석하지 않습니다.
GPU 사용률·온도·전력은 장치 전체 값이고, 공유 GPU의 계산 사용률을 사용자 메모리 비중으로 배분하지 않습니다.
어떤 로그에도 등장하지 않는 노드는 전체 노드 용량에 포함할 수 없습니다.

## 검증

```bash
python3 -m unittest discover -s tests -v
python3 HPC_Monitor_Web.py --self-test
python3 HPC_Monitor_X11.py --self-test
```

Node.js가 있으면 Python과 기존 JavaScript 엔진의 동일 예제·기간·필터별 결과를 비교하는 테스트도 실행합니다.
Node.js는 실제 Python GUI 실행에 필요하지 않습니다.
Matplotlib가 설치되어 있으면 각 그래프를 화면 없는 환경에서도 PNG로 렌더링해 검증합니다.
원격 PC의 X 서버와 SSH 설정은 사용 환경에서 별도로 확인해야 합니다.

## 구성

- `hpc_monitor_core.py`: 표준 라이브러리 기반 TSV/JSON 파서와 공통 집계 엔진.
- `hpc_monitor_plot.py`: X11 GUI용 Matplotlib 그래프.
- `HPC_Monitor_X11.py`: Tkinter GUI와 비동기 파일·집계 처리.
- `HPC_Monitor_Web.py`: Python HTTP/API 서버와 기존 화면을 연결하는 웹 GUI.
- `HPC_Usage_Monitor.html`: 독립 실행 HTML 원본.
- `tests/`: 경계 조건, HTTP 입출력, 그래프 렌더링 및 Python/JS 결과 비교.

현재 배포: `main`, 보존 브랜치: `ver-1.0`.
구현 참고: [Python Tkinter](https://docs.python.org/3/library/tkinter.html),
[Matplotlib backends](https://matplotlib.org/stable/users/explain/figure/backends.html).
