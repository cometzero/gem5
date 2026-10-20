# LPDDR5X 부품 근거: 부분 적용

`MicronY52Q_8533_1x16_BG_BL32_Partial`은 선택 가능한 **부분 문서화 모델**이다.
실제 Zena 탑재 부품은 확인되지 않았다. 기존 sensitivity class와 기본 실행은 변경하지 않는다.

근거는 Micron의 [Y52Q Automotive LPDDR5X Rev. G, 2025-05](https://mm.digikey.com/Volume0/opasdata/d220001/medias/docus/8885/561b-y52q-ddp-auto-lpddr5x.pdf)이며 선택 부품은 `MT62F512M64D2CZ-023 AAT:E`다.

| 근거 위치 | 확인 항목 | 모델 적용 |
|---|---|---|
| p.1–2 | -023, 8533 Mb/s/pin; WCK:CK 4:1 지원 | 기존 nominal CK 0.9375 ns 유지 |
| p.10 Tables 2/3 | 패키지 4 channels × x16, single rank, channel당 8 Gbit | channel당 device_size 1 GiB, rank 1 |
| p.10 Table 3 | 4 bank groups × 4 banks, page 2048 bytes, bank당 32768 rows | BG 4, banks 16, row buffer 2 KiB |
| p.11 Table 4 | all-bank refresh cycle 210 ns | tRFC 210 ns |
| p.1, p.10 | BL16/32 지원, native burst 16 | 기존 BL32 선택 유지; 새 타이밍을 추정하지 않음 |

이 문서는 완전한 -023 AC timing table이 아니다. 일반 규격 문서 2/3을 별도로
참조하므로 tCL/tCWL/tRCD/tRP/tRAS/tWR/tREFI 및 온도별 refresh 조건을 여기서
확정할 수 없다. 해당 값과 CK 기반 burst/turnaround timing은 기존 class를
상속하며 **부품 보정 완료 값으로 표시하지 않는다**. multi-rail power도 적용하지 않는다.

## 2 GiB 노출 범위와 실제 밀도의 차이

현재 backend의 synthetic `device_size=2GiB/channels` override를 유지하면
새 class의 부품 밀도가 지워진다. 부분 부품 profile은 해당 override를 생략해야 한다.

또한 native `src/mem/dram_interface.cc`는 `rowsPerBank`를 `device_size`가 아니라
노출된 `AddrRange` 용량으로 계산한다. 기존 `RoRaBaCoCh` decoder는 channel,
bank/rank/column bit를 제거한 뒤 이 row 수로 modulo한다.

| 채널 | 2 GiB 중 채널당 노출 | native row 수/bank | 부품 row 수/bank |
|---|---:|---:|---:|
| 4 | 512 MiB | 16384 | 32768 |
| 8 | 256 MiB | 8192 | 32768 |
| 16 | 128 MiB | 4096 | 32768 |

2 GiB 한정 실행은 물리 채널의 일부 주소만 사용하는 대조로 취급한다. 사용하지 않는
상위 row의 동작은 검증하지 않는다. 전체 geometry를 노출하려면 채널당 1 GiB,
즉 4/8/16 GiB가 필요하지만 현재 기본 RAM을 임의 확대하지 않는다. density 경고를
숨기거나 refresh 시간을 노출 용량에 비례해 줄이지 않는다.

## 실제 검증

```sh
# workspace root
 gem5/build/ARM/gem5.opt -d build/gem5-cmn/lpddr5x-datasheet/native \
   gem5/tests/zena_cmn/directed/lpddr5x_documented.py
```

3개 검사가 geometry, 미확인 parameter 보존, 노출 범위별 row 산식을 확인했다.
1 GiB 채널의 native MemCtrl/DRAMInterface instantiate와 1 tick 실행도 통과했다.
로그: `build/gem5-cmn/lpddr5x-datasheet/native.log`.
이는 DRAM 트래픽·성능·실리콘 일치 검증이 아니다. Stage 5의 전체 timing 보정은
추가 일반 규격과 실제 목표 부품·controller 정책 확인 전까지 미완료다.

## Native backend 실제 트래픽 검증

`directed/lpddr5x_backend_traffic.py`는 실제 `build_memory_backend()`를 호출하고
`preserve_device_geometry=true`로 1 GiB device/rank 설정을 보존한다. 2 GiB
노출 범위는 유지한다. 채널별 독립 IOXBar(64 B, 2 GHz)와 generator 하나를
두며 요청은 64 B, 주기 2 ns, outstanding 최대 64다. 주소 stride는
`channels × 64 B`여서 각 generator가 해당 controller만 사용한다.
이 배치는 CPU·Ruby·공유 mesh 성능을 검증하지 않는다.

10 µs warm-up 뒤 20 µs ROI를 reset/dump했다. 나중의 자동 종료 dump는
제외했다. 기존 바이너리 `build/gem5-zena/calibration-binary/gem5-before-exclusive.opt`로
실행했으며 결과는 다음과 같다.

| 채널 | Read burst bytes | Read GB/s | Write burst bytes | Write GB/s | nominal raw GB/s |
|---|---:|---:|---:|---:|---:|
| 4 | 1,279,232 | 63.9616 | 1,261,056 | 63.0528 | 68.2667 |
| 8 | 2,558,464 | 127.9232 | 2,522,112 | 126.1056 | 136.5333 |
| 16 | 5,116,928 | 255.8464 | 5,044,224 | 252.2112 | 273.0667 |

Raw 값은 nominal 8533.333 MT/s × x16 × 채널 수로 계산했다. Read 약93.69%,
write 약92.36%이며 독립적으로 동일 부하를 공급하므로 1:2:4로 확장된다.
이는 실제 CPU나 target controller의 달성치를 뜻하지 않는다.

모든 채널의 서비스 byte가 양수이고 `DRAM bytes = bursts × 64 B`이며
메모리 밖으로 suppress된 요청은 0이다. generator response byte와 DRAM
burst byte는 ROI 경계의 outstanding 때문에 일치할 필요가 없다. Write
response를 물리 DRAM 기록 완료로 해석하지 않는다.

16채널 read에서 기존 synthetic density profile도 **5,116,928 B /
255.8464 GB/s**로 동일했다. 실제 range와 native decoder row 수, timing,
scheduler가 같고 `device_size` metadata만 달라진 대조이므로 합당한 결과다.
`tRFC=210 ns`, `tREFI=3.9 µs`를 유지했다. 후자는 이 부품·온도 조건에서
새로 확인된 값이 아니다. 근거가 없는 timing sensitivity 값을 추가하거나
refresh를 꺼서 수치를 높이지 않았다.

```sh
build/gem5-zena/calibration-binary/gem5-before-exclusive.opt \
  -d build/gem5-cmn/lpddr5x-datasheet/traffic16read \
  gem5/tests/zena_cmn/directed/lpddr5x_backend_traffic.py --channels 16
# write: --read-percent 0; 기존 class 대조: --synthetic
python3 gem5/tests/zena_cmn/check_backend_traffic.py \
  build/gem5-cmn/lpddr5x-datasheet/traffic16read \
  --output build/gem5-cmn/lpddr5x-datasheet/traffic16read-check.json
```

전체 7회 결과: `build/gem5-cmn/lpddr5x-datasheet/traffic-summary.json`.
각 실행에 config/stats/traffic-manifest가 있다. 이 PASS는 native 요청·burst
회계와 backend 연결 검증이며, 부품 timing 보정이나 읽은 데이터의 정답 검사는 아니다.

### 16채널 read/write 50/50 대조

같은 generator·주소·finite outstanding·timing에서 `--read-percent 50`만
변경했다. 생성 단계의 확률적 50/50이며 ROI 서비스 byte의 정확한 1:1은 아니다.

| 20 µs ROI 관측 경계 | Read bytes | Write bytes | 합계 bytes |
|---|---:|---:|---:|
| Generator가 받은 응답 | 2,071,552 | 1,977,344 | 4,048,896 |
| DRAM 서비스 burst | 2,079,744 | 1,976,320 | 4,056,064 |

실제 생성은 63,808 requests = 4,083,712 B다. backpressure가 없는 nominal
공급은 `16 × 20µs/2ns × 64B = 10,240,000B`지만 이를 실제 발행량으로
간주하지 않는다. Native `numPackets`는 최초 전송 시도 전 증가하며 retry 대기
요청을 포함할 수 있다. 이 generator에는 애플리케이션 유효 처리량이라는 지표가 없다.

합산 DRAM 대역폭은 **202.8032 GB/s**(read 103.9872, write 98.8160)로
nominal raw의 74.26875%다. 순수 read/write보다 낮아지며 방향 전환과
controller read/write scheduling이 포함된 효과다. 개별 원인의 기여율을
분리 측정했다고 주장하지 않는다.

ROI 뒤 2 µs 동안 새 생성 요청은 0이지만 read 33,792 B, write 51,200 B가
추가 서비스되었다. 따라서 ROI 끝에 미완료 downstream 작업이 남았음을
확인했다. controller의 순간 residual queue entry는 native stats에 없어
JSON null/NOT_EXPORTED로 기록하며 평균·arrival histogram으로 대체하지 않는다.

`src/mem/mem_ctrl.cc:addToWriteQueue()`는 write를 queue에 넣은 뒤
`accessAndRespond(pkt, frontendLatency, ...)`를 호출한다. 실제 DRAM
서비스를 기다리지 않으므로 write 응답은 DRAM 완료가 아니다. 이 실행의
frontend latency도 기존 10 ns다. 잔여 write 51,200 B 역시 서비스 회계이며
모든 DQ 전송의 물리적 완료를 독립 관측한 값은 아니다.

결과: `build/gem5-cmn/lpddr5x-datasheet/traffic16mixed-summary.json`.
동일 검사기로 mixed와 순수 read/write 3개 결과를 재검증했고 모두 PASS다.
