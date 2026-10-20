# 유한 LPDDR5X 트래픽의 native drain 검사

`tests/zena_cmn/directed/lpddr5x_backend_traffic.py --drain-completion`은
기존 10–30 μs ROI를 유지하면서 30 μs에서 native `m5.drain()`을 수행한다.
기본 모드의 2 μs tail 관측과 구별되는 선택 기능이다.

- 10 μs 통계를 먼저 저장하고 reset하므로 warm-up에서 시작해 ROI에서 끝난
  요청도 생애 누계에 정확히 포함한다.
- 30 μs ROI 통계를 저장한 뒤 drain을 호출한다. BaseTrafficGen의 drain은
  새로운 생성 이벤트를 중단하고 pending retry를 처리한다.
- MemCtrl의 drain은 read/write/response queue와 DRAM rank drain을 기다린다.
  `MemCtrl::drain()`은 대기 write를 진행시키고 `allIntfDrained()`를 검사한다.
  DRAM의 `allRanksDrained()`는 rank power/ref idle 상태를 요구한다.
- drain 반환 후 통계를 별도로 저장하며 ROI 시작/끝, drain 시작/끝 tick과
  각 dump index를 manifest에 기록한다. 종료 시 자동 dump를 ROI로 오인하지 않는다.

Checker는 모든 channel에서 생성 수=완료 read+write 수, 생성 수의 drain 중
불변성, suppressed=0, 완료 bytes=요청 수×64를 검사한다. Controller가 수락한
`readReqs/writeReqs/bytesReadSys/bytesWrittenSys`의 생애 누계도 동일해야 한다.
양 방향의 DRAM byte counter가 모든 channel에 존재해야 하며 ROI 이후 증가량을
별도로 보고한다. write 병합/forwarding 때문에 DRAM burst bytes를 응답 bytes와
일률적으로 동일하다고 강제하지 않는다.

이 결과는 native 모델의 queue/rank drain 완료다. dirty cache flush를 포함하지
않고 실제 silicon의 DQ 신호 종료 시점을 증명하지도 않는다. 또한 write 응답이
도착했다는 사실만으로 DRAM 완료라고 판단하지 않는다.

## 재현

Workspace root에서 실행하며 출력은 신규 디렉터리를 사용한다.

```sh
gem5/build/ARM/gem5.opt --outdir=build/gem5-zena/drain-write-new \
  gem5/tests/zena_cmn/directed/lpddr5x_backend_traffic.py \
  --channels 16 --hynix --read-percent 0 --drain-completion
python3 gem5/tests/zena_cmn/check_backend_traffic.py \
  build/gem5-zena/drain-write-new --output /tmp/drain-write-check.json
python3 -m unittest discover -s gem5/tests/zena_cmn -p test_backend_drain.py -v
```

Mixed 대조는 `--read-percent 50`을 사용한다. Unit 검사 9개는 cross-reset 정상
누계와 pending response, 추가 생성, controller 수락 불일치, counter 누락,
false drain, 잘못된 dump/절대 tick/주파수의 거부를 검사한다. Native 실행 결과는 아래와 같다.


## Native 실행 결과 (2026-10-18)

`build/gem5-zena/improvements-20261018/drain-results.json`에서 두 실행 모두
PASS다. 16채널 Hynix public timing candidate, 64 B 요청, ROI 10–30 μs다.
전 채널 생성/완료/수락 회계 및 dump의 절대 tick/주파수를 검사했다.

| 모드 | drain 시작 tick | drain 완료 tick | 추가 시간 | 생애 생성=완료 요청 | drain 중 추가 DRAM Read / Write bytes |
|---|---:|---:|---:|---:|---:|
| write-only | 30,000,000 | 30,369,546 | 369.546 ns | 117,920 | 0 / 66,560 |
| mixed 50% | 30,000,000 | 30,519,286 | 519.286 ns | 91,952 | 56,320 / 59,392 |

Write-only 생애 controller 수락/응답/DRAM write bytes는 각각 7,546,880 B다.
Mixed 생애 read는 2,974,720 B, write는 2,910,208 B이며 이 실행에서는 수락,
응답, DRAM burst byte가 일치했다. 이 일치는 모든 workload에 일반화하지 않는다.
고정 ROI DRAM 처리량은 각각 248.4224 GB/s, 196.2496 GB/s이며 drain 추가
시간을 ROI 분모에 섞지 않았다. 기존 임의 tail 길이에 의존하지 않고 native
queue/rank drain 완료까지 진행한 결과다.

실행 디렉터리는 `drain-write`, `drain-mixed`, 로그는 동일 경로의 `.log`다.
검증 범위는 **NATIVE_BACKEND_TRAFFIC_ONLY_NOT_PART_OR_CPU_CALIBRATION**이다.
CPU·cache·Linux write completion 또는 실제 LPDDR5X 부품의 물리적 완료를
검증한 결과로 해석하지 않는다.
