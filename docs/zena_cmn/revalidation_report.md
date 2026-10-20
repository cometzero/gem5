# 실패 항목 조사 및 재검증

검증일: 2026-10-14. 기존 실패 로그를 보존하고 새 실행 디렉터리를 사용했다.
원래 실패 목록은 `build/gem5-zena/cmn-revalidation/original-failure-inventory.json`에 있다.

## 항목별 원인과 조치

| 이전 항목 | 조사 결과 | 재검증 방법 |
|---|---|---|
| `cmn-linux4-disk-03`, `cmn-linux16-quick` assertion | LQ64가 허용한 같은 line의 read 49개가 native IEW writeback 48개 슬롯을 초과 | LQ32 기본값, LQ64 사전 거절 검사, 원래 4코어 실패 입력 그대로 재실행 |
| `cmn-linux4-disk` Permission denied | simulator를 링크 중인 상태에서 실행한 준비 순서 문제 | 빌드 완료·실행 권한 확인 후 동일 바이너리로 Linux/디스크/종료 실행 |
| `cmn-linux4-disk-02` 종료 신호 | 1 MiB smoke로 교체하며 중단한 실행. 모델 결함 판정 대상 아님 | 중단 이력 유지, 새 디렉터리에서 완주 검증 |
| `cmn-linux16` 64 MiB timeout | O3 first-touch/warmup 단계에서 host 1200초 소진, ROI 미도달 | 64 MiB와 모든 cache line 접근을 유지하고 검증용 중복 word 접근 제거, 진행 로그 추가 |
| Linux cache sysfs unavailable | DT가 4 leaves를 할당하지만 native CLIDR의 cache-type 필드가 모두 0인 arm64 경로가 3 leaves만 채움 | 실제 DTB 및 현재 소스로 불일치 재현, UNSUPPORTED로 명시 |

## Assertion 회귀

원래 입력과 `cmn-revalidation/lq32-disk4`의 비교 파일:
`build/gem5-zena/cmn-revalidation/lq32-disk4/original-failure-comparison.json`.

이 재실행은 원래 실패 당시 guest ELF/initramfs를 포함한 7개 입력 SHA256,
guest 명령(`4 workers, 1 iteration, 1 MiB`), DTB hash가 동일하다. 실제
config 차이는 CPU의 LQ64→32와 실행 디렉터리/terminal port뿐이다.
이전 보고서의 2-iteration 재검증보다 엄격한 단일 설정 대조다.
**415.079초, exit code 0, 전체 PASS**이며 디스크 재마운트·내용 비교와
PSCI SYSTEM_OFF까지 완료했다. base disk SHA256도 실행 전후 동일하다.

`tests/zena_cmn/directed/cpu_writeback_guard.py`는 현재 gem5의 실제 CPU
SimObject로 LQ32 생성과 LQ64 사전 거절을 검사한다. 이 짧은 검사의
PASS는 simulation 실행 증거가 아니며 Linux 회귀 실행과 구분한다.

C++/protocol 수정이나 writeback buffer 확대는 적용하지 않았다.
LQ32의 검증된 경로를 지원하며 모든 임의의 queue 조합을 보증하지 않는다.

## 64 MiB 시간 초과 조사

기존 workload는 cache line의 모든 8 B word를 초기화·변경·검사했다.
이번 기능 검사는 cache line의 eviction/정합성을 확인하므로 64 B 정렬
영역에서 **매 cache line의 8 B word 하나**를 검사하도록 변경했다.

- 전체 주소 footprint: 67,108,864 B = 64 MiB.
- 서로 다른 cache line: 1,048,576개, 하나도 생략하지 않는다.
- line당 검사 데이터: 8 B. 전체 word 무결성 검사와는 다른 범위다.
- read/copy/STREAM 성능 workload와 DRAM/CPU/cache 파라미터는 바꾸지 않았다.
- 실행 JSON에 footprint, line 수, stride, 검사 byte 수를 명시한다.

진행 로그로 O3 전환, first-touch, warmup 완료, ROI 및 정답 검사를
구분한다. 4 KiB 익명 페이지의 kernel zeroing은 사용자 접근을 line당
1 word로 줄여도 남는다. 별도 `--prefault-before-switch` 실행은 전체
영역의 페이지를 Atomic noncaching 단계에서 준비하고, O3 전환 후 모든
line을 다시 초기화하여 cache warmup과 ROI를 수행한다. 기본값은 O3
first-touch이며, direct-timing 또는 streaming 모드와 이 옵션의 조합은
거절한다. 페이지 준비/물리 배치가 다르므로 두 실행을 동일 조건의 성능
비교로 해석하지 않는다. 실제 controller eviction/writeback 통계도 확인하여 단순한
working set 크기만으로 eviction 완료를 주장하지 않는다.

warmup에서 worker 0 진행 메시지가 오래 바뀌지 않아 SIGUSR1 통계 dump를
두 번 비교했다. SIGUSR1은 dump만 수행하며 reset하지 않는다. 68 host초
동안 tick은 355.699 µs 증가했고 CPU 9–15의 committed instructions와
DRAM read 4,777,920 B / write 4,831,872 B가 증가했다. CPU 0–8의 해당
카운터 증가는 0이었다. 따라서 worker 0 로그 정지만으로 deadlock으로
판정하지 않았으며, worker별 진행 차이를 확인했다. 상세 수치는
`cmn-linux16-line64-prefault/warmup-diagnostic.json`에 있다. 이 진단
warmup dump들과 추가한 진행 확인용 partial ROI dump는 정확한 begin/end
tick으로 선택하는 최종 ROI 집계에서 제외한다. partial ROI의 16코어 명령
증가와 DRAM 트래픽은 `roi-progress-diagnostic.json`에 별도로 보존했다.

## cache sysfs 제한

실제 DTB를 읽는 진단 probe와 3개 단위 검사를 추가했다.
[원인과 올바른 후속 수정 범위](cache_visibility.md)를 참조한다.

L1을 허위 unified cache로 표시하거나 MIDR/CLIDR 상수만 바꾸지 않았다.
현재 결과는 **UNSUPPORTED**이며 cache visibility PASS가 아니다. 올바른
ISA cache ID/maintenance 구현 또는 Linux의 firmware fallback 수정은
별도 회귀 검증이 필요하다.

## 실행 결과

| 검증 | 결과 | 증거 |
|---|---|---|
| 원래 4 CPU/4채널/1 MiB/1 iteration + disk 재현 | **PASS**, host 415.079 s, exit 0 | `cmn-revalidation/lq32-disk4/result.json`, `original-failure-comparison.json` |
| 실제 SimObject LQ32 생성 / LQ64 사전 거절 | **PASS** (simulation 미실행) | `cmn-revalidation/lq64-guard/guard-result.json` |
| host C 정답/라인 범위 검사 | **16 PASS**: workers 2/4/8/16 × 1/64 MiB × pre-fault on/off | `build/gem5-cmn/guest-host/line-coverage-check.json` (workspace 기준) |
| Python 설정·ROI·DT·cache 진단 및 옵션 거절 | **30 PASS** | `cmn-revalidation/python-tests.log` |
| native gem5 주소/MC/CPU 설정 | **7 PASS** | `cmn-revalidation/native-address.log` |
| 변경 Python 파일 Black 검사 | **PASS**, 5 files | `cmn-revalidation/format.log` |
| 16 CPU/64 MiB line-only, O3 first-touch | **STOPPED_BY_TEST_CHANGE**, host 675.065 s, ROI 미도달 | `cmn-linux16-line64/result.json` |
| 16 CPU/64 MiB, Atomic page pre-fault | **전체 FAIL(TIMEOUT)**, host 3,602.952 s. 전체 line checksum·ROI PASS, 정상 종료 미확인 | `cmn-linux16-line64-prefault/result.json`, `diagnosis.json` |
| 실제 16 CPU DT + CLIDR 진단 | **UNSUPPORTED**, leaf 불일치 재현 | `cmn-linux16-line64-prefault/m5out/cache-visibility.json` |

증거 경로의 기준은 workspace `build/gem5-zena/`다. Python 전체 discovery는
native `m5` 모듈이 필요한 `test_address_map.py`를 일반 Python에서 import할 수
없으므로 검사를 분리했다. 위 native 7개는 실제 gem5 executable로 통과했다.

새 guest는 AArch64 static C11/pthread로 빌드했다.
실행의 `guest/build-manifest.txt`와 `guest-build.log`에 compiler, flags,
소스/바이너리 SHA256을 기록했으며 현재 guest C 소스와 실행 입력이 동일함을
확인했다. host 16개 결과는 C 로직 검사이며 gem5 정합성 검증과 구분한다. gem5 C++/SLICC 변경은 없어
기존 multi-protocol CHI 바이너리를 재사용했다. 기존 Classic 실행 코드와
LPDDR5X patch는 보존했다. 재현 가능한 기본 실행 방법과 성공 판정은
[Quick Start Guide](quick_start.md)에 정리했다.

## 64 MiB 최종 결과와 남은 제한

16 CPU/16채널, LQ32, 2 iterations, 명시적 Atomic page pre-fault 조건에서
전체 1,048,576개 line의 warmup과 변경·읽기·checksum 검사를 완료했다.
`CMN_RESULT`는 같은/교차 cluster 검사와 함께 PASS를 출력했다.
정확한 ROI는 **76,780,077,000 → 81,595,310,000 tick = 4.815233 ms**다.

| 관측 카운터 | L2 합계 | DSU 합계 | Home 합계 |
|---|---:|---:|---:|
| inbound WriteBackFull transaction samples | 1,063,123 | 812,974 | 1,304,765 |
| Local_Eviction transaction samples | 1,529,657 | 1,487,946 | 미제공 |
| outgoing SendWriteBackOrWriteEvict samples | 833,552 | 1,304,765 | 미제공 |

DRAM read는 **134,736,768 B / 2,105,262 bursts**, write는
**91,828,736 B / 1,434,824 bursts**다. 계층별 transaction 수는 같은
트래픽을 서로 다른 경계에서 관측하므로 합산해 unique line 수로 해석하지
않는다. 미제공 카운터는 0이 아닌 null로 보존했다. 첫 5개 진단 dump를
제외하고 정확한 ROI 종료 dump(0-based block 5)를 선택했다.
이는 실제 eviction/writeback을 동반한 sampled dirty-line 정합성 증거이며
대역폭 benchmark나 전체 dirty data의 DRAM 기록 완료 증거가 아니다.

그러나 PASS 출력 이후 `CMN_EXIT=0`과 정상 poweroff를 확인하기 전에
**host 3600초 예산이 소진됐다**. harness의 원본 `result.json`은
FAIL/exit -9로 유지했다. 소스상 PASS 뒤에는 munmap/free 및 process 종료가
있지만, 그중 어느 지점이 시간을 소비했는지는 PC trace로 확정하지 않았다.
따라서 "종료까지 해결" 또는 "전체 실행 PASS"라고 결론 내리지 않는다.

남은 검증은 충분한 별도 종료 예산에서 정상 종료를 확인하고, 필요하다면
post-ROI munmap/free/process teardown 구간을 계측하는 것이다. 이번 실행을
시간 제한 없이 연장하거나 종료를 건너뛰는 m5_exit로 대체하지 않았다.
짧은 4코어 disk 회귀에서는 정상 종료까지 이미 PASS이며, 이 증거를 대용량
16코어 실행의 정상 종료 증거로 대체하지 않는다.
