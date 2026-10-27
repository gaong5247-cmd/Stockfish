# 첫 Fishtest 패치 준비

## 현재 판정: 실험 브랜치

재현된 초기화 잠금 장애는 해결됐고 기본 bench 및 확인한 탐색 결과는 동일하다.
그러나 **검색 속도 무하락은 입증되지 않았다.** 아래의 두 시작 수정 비교 모두
평균 속도가 기준보다 낮았다. 이 브랜치를 성능 검증 완료판으로 취급하거나
기존 `master`에 자동 병합하지 않는다. 공식 테스트도 아직 제출하지 않았다.

| 실험 | 검색 속도 변화의 기하평균 | 근사 95% 구간 | 판단 |
| --- | ---: | --- | --- |
| 조용한 수의 pawn-history 주소 계산 이동 | -0.033% | -1.59% ~ +1.55% | 이득이 확인되지 않아 적용 제외 |
| 초기화 수정, 일반 AVX2+LTO 빌드 | -1.742% | -3.57% ~ +0.12% | 무하락 미확인 |
| 초기화 수정, 외부 NNUE AVX2+LTO+PGO | -2.255% | -4.98% ~ +0.55% | 무하락 미확인 |

각 비교는 GCC 13.3.0, Xeon Platinum 8573C의 가상화 환경에서 CPU 0에 고정하고
8개 AB/BA 교대 쌍을 측정했다. 작은 표본과 한 대의 호스트로부터 얻은 설명적
구간이며, 다른 CPU나 Elo에 대한 결론이 아니다. 느린 표본도 삭제하지 않았다.
각 실행의 raw 측정값과 바이너리 해시는 `validation-2026-10-27.json`에 기록한다.

시작 수정은 정상적인 잠금 획득 시 탐색 경로를 바꾸지 않지만 바이너리 배치,
메모리 배치, 호스트 부하 등의 영향까지 없다고 단정할 수 없다. 이 측정으로는
“검색이 빨라졌다” 또는 “전혀 느려지지 않았다”를 주장하지 않는다.

## 변경 범위

- 저장소: https://github.com/gaong5247-cmd/Stockfish
- 테스트 브랜치: `my-first-test`
- 기준 커밋: `ff3a6c508ed731f0dcf253f4ddf646df19a62f84`
- 기준 기본 bench: `2389370`
- 수정 후 기본 bench: `2389370`
- 대상: NNUE 공유 메모리의 초기화 잠금에서 발생할 수 있는 무기한 대기.

Windows의 `WaitForSingleObject(..., INFINITE)`와 Unix의 blocking `flock`
대기를 최대 약 1초로 제한한다. 잠금을 얻지 못하면 이미 존재하는 개별 메모리
할당 경로가 **동일한 NNUE 데이터**를 복사해서 사용한다. 잠금이 즉시 획득되는
Unix 경로에는 시계 조회나 sleep을 추가하지 않는다.

탐색 상수, 평가 함수, NNUE 파일, `Move Overhead`, UCI 응답 시점은 변경하지
않는다. Windows의 abandoned mutex는 획득한 소유권을 해제한 후 안전하게
fallback한다. timeout 오류에 무관한 `GetLastError()` 값을 표시하지 않는다.

이 변경은 **개별 초기화 잠금의 무한 대기**를 없앤다. NNUE 읽기, 메모리 할당,
여러 NUMA 복제, peer socket 응답, 운영체제 부하를 합친 전체 기동 시간이
1초 이내라는 뜻은 아니다. 오래 지속되는 정상적인 경쟁에서도 fallback이
발생할 수 있어 공유 메모리 사용보다 프로세스별 메모리 소비가 늘 수 있다.

## 검증 원칙

1. 기준과 수정본을 같은 컴파일러·아키텍처·플래그로 빌드한다.
2. 기본 `bench`의 `Nodes searched`와 각 포지션의 score/PV/node 결과를 비교한다.
3. 실제 UCI 기동, 옵션 변경, 검색 중 `isready`, `stop`, `quit`를 확인한다.
4. 초기화 잠금을 다른 프로세스가 계속 보유하는 장애를 재현하고, 수정본이
   `uciok`와 `readyok`를 보낸 뒤 실제 `bestmove`까지 계산하는지 확인한다.
5. 내장 NNUE 빌드와 Fishtest가 사용하는 외부 NNUE 빌드를 구분해 확인한다.
6. 속도는 번갈아 실행한 여러 bench로 비교하며, 측정 오차 안의 차이를
   개선으로 발표하지 않는다. 기력 향상은 별도의 대국 검증 전에는 주장하지 않는다.

기본 bench의 일치는 해당 테스트 포지션의 재현성 근거다. 모든 가능한 체스
상황에서 동작이 동일하다는 증명이나, 모든 CPU에서의 속도·Elo 무하락 보장은 아니다.

## 로컬 실행

```sh
cd src
make -j4 build ARCH=x86-64-avx2 COMP=gcc
./stockfish bench
cd ..
python3 tests/fishtest_preflight.py src/stockfish --expected-bench 2389370
python3 tests/startup_lock.py src/stockfish
```

`startup_lock.py`는 Unix 공유 메모리와 Unix socket이 실제로 사용 가능한
환경에서 실행한다. 제한된 컨테이너가 socket 생성을 막으면 이 경로를 검사할 수
없으므로 테스트가 명시적으로 실패한다.

수정 전 바이너리로 무한 대기를 재현하려면:

```sh
python3 tests/startup_lock.py /path/to/base-stockfish --expect-timeout
```

외부 NNUE 파일을 별도 폴더에 두었다면 `--working-directory /path/to/net-folder`를
지정한다. 테스트용으로 복사한 바이너리만 사용하며, 다른 엔진의 공유 메모리
파일이나 잠금을 제거하지 않는다.

Windows의 mutex timeout·짧은 경쟁·abandoned 소유권 경로는 다음처럼 검사한다.

```sh
g++ -std=c++17 -O2 -pthread tests/startup_lock_windows.cpp -o startup-lock-windows.exe
./startup-lock-windows.exe
```

Fishtest 방식의 PGO·외부 NNUE 빌드는 아래처럼 플래그를 **환경변수**로 전달한다.
`make ... CXXFLAGS=...`로 전달하면 Makefile의 최적화·아키텍처 플래그를
덮어쓸 수 있다.

```sh
cd src
make clean
env CXXFLAGS='-DNNUE_EMBEDDING_OFF -Werror' make -j4 profile-build ARCH=x86-64-avx2 COMP=gcc
./stockfish bench
```

## Fishtest 제출 전

Fishtest는 공식 Stockfish 개선을 검증하는 공용 자원이다. 현재 공식 master에
기반한 작은 변경을 GitHub fork의 별도 브랜치에서 준비한다. 독립적으로 갈라진
엔진 전체를 공식 서버에서 대전시키는 용도로 사용하지 않는다.

- 한 테스트에는 하나의 아이디어를 넣는다.
- 기존 테스트 이력에서 같은 아이디어를 확인한다.
- 이 패치는 초기화 안정성 수정이다. 공식 개발진에게 재현 절차와 차이를
  보여 주고, 버그 인정 및 필요한 회귀 검증 방식을 먼저 협의한다.
- `non-regression` bounds를 임의로 선택하지 않는다. 버그 수정 등의 예외는
  문서상 사전 협의가 요구된다.
- Elo 향상을 목표로 하는 탐색 변경은 별도 브랜치에서 STC, 통과 후 LTC로 검증한다.
- 표준 SPRT를 중간 결과가 좋아 보인다는 이유로 임의 종료하지 않는다.
- 통과 결과도 자동 병합을 의미하지 않는다.

### 이 브랜치의 입력값

| 항목 | 값 |
| --- | --- |
| Test repository | `https://github.com/gaong5247-cmd/Stockfish` |
| Test branch | `my-first-test` |
| Test signature | `2389370` |
| Base branch / commit | `ff3a6c508ed731f0dcf253f4ddf646df19a62f84` |
| Base signature | `2389370` |
| Info | `Bound NNUE shared-memory initialization lock waits; use the existing local-memory fallback on timeout. No functional search change.` |

기준 master가 바뀌었다면 제출 전에 변경을 새 기준에 적용하고 두 bench를 다시
계산해야 한다. 표의 숫자를 다른 버전에 그대로 복사하지 않는다.

### 일반적인 기력 실험의 설정

아래는 2026-10-27에 확인한 공식 Fishtest 프리셋이며, 위 초기화 수정에 대해
어떤 테스트를 실행할지는 개발진 협의 결과를 따른다.

| 종류 | TC | Threads | Hash | SPRT bounds |
| --- | --- | ---: | ---: | --- |
| Standard STC | `10+0.1` | 1 | 16 MB | `[0.0, 2.0]` |
| Standard LTC | `60+0.6` | 1 | 64 MB | `[0.5, 2.5]` |
| Non-regression | 해당 TC | 해당 설정 | 해당 설정 | `[-1.75, 0.25]` |

SPRT 통과는 통계적 판단이며 실제 Elo가 절대로 낮지 않다는 수학적 보장은 아니다.

## 공식 자료

- [첫 테스트·기여 규칙·테스트 방법](https://official-stockfish.github.io/docs/fishtest-wiki/Creating-my-first-test.html)
- [Fishtest FAQ](https://official-stockfish.github.io/docs/fishtest-wiki/Fishtest-FAQ.html)
- [UCI와 Stockfish 명령](https://official-stockfish.github.io/docs/stockfish-wiki/UCI-Protocol-and-Stockfish-Commands.html)
- [실제 Fishtest 폼의 프리셋](https://github.com/official-stockfish/fishtest/blob/5a8a229a521b4eaab2ecf19b92f0d2fd6c3be13b/server/fishtest/templates/tests_run.html.j2)
- [Fishtest worker의 build·bench·기동 확인](https://github.com/official-stockfish/fishtest/blob/5a8a229a521b4eaab2ecf19b92f0d2fd6c3be13b/worker/games.py)
- [Fastchess의 startup·ping·newgame timeout](https://github.com/Disservin/fastchess/blob/master/man.md)
- [테스트 생성 페이지](https://tests.stockfishchess.org/tests/run)

`Move Overhead`는 대국 중의 시간 여유에 관한 설정이다. 엔진 기동 지연 해결책으로
증가시키면 실제 탐색에 사용할 시간이 줄어들 수 있다. 기동 timeout, `readyok`
응답 timeout, 실제 대국 시간패를 따로 검사해야 한다.
