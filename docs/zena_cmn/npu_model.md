# NPU v0.1 timing model 계약

대상: `src/dev/npu/NpuV01.py`, `npu_v01.hh`, `npu_v01.cc`.
이 모델은 기존 NPU v0.1의 package/context/MMIO ABI를 실행하는 **추상 제어
엔진**이다. RV32 PC/GPR/RVV 명령이나 실물 NPU의 cycle-accurate 구현이 아니다.
Linux 통합·최종 실행 근거는 상위 프로젝트의 NPU 구현 보고서에서 관리한다.

## 기본 ABI와 기능

- 64 KiB MMIO, DEVICE_ID `0x00010001`, completion/fault IRQ를 각각 제공한다.
- Package ABI 1.0, context ABI 2.0, little-endian FUNC_OPS를 사용한다.
- 기본 variant 1은 END, DMA_LOAD, DMA_STORE, IME_MMA_I8만 실행한다.
- Package → context → binding 순서로 실제 DMA fetch한 뒤 전체 op stream을
  preflight한다. 잘못된 후속 op 때문에 앞선 payload DMA가 실행되지 않는다.
- 한 job의 기존 제한을 유지한다: 4096 op, payload DMA 64 MiB,
  누적 DMA page 32768개, 16 Mi MAC. Metadata DMA는 payload byte 제한/카운터와
  별도이며, package는 최대 1 MiB이다.
- Capability는 기본 CAP0 `0x202`, CAP1 `0x102`다. 두 IME 작업 분할과
  INT8 MMA, 2 MiB SRAM 및 한 직렬 DMA 실행 경로를 표시한다.
  RVV 및 하드웨어 barrier 지원을 광고하지 않는다.
- `HART_MASK=1/2`는 한 IME, `3`은 두 IME에 독립 output tile을 배분한다.
  한 tile만 있는 GEMM을 hart 수만으로 두 배 빠르게 계산하지 않는다.

## SRAM, IME 및 DMA의 시간 모델

| 자원 | 모델 계약 |
|---|---|
| SRAM | CPU aperture 없는 2 MiB byte storage |
| Bank | 16개, 4 B interleave, `bank=(offset/4)%16` |
| Bank port | bank당 공용 read/write 1 beat/cycle, response 2 NPU cycles |
| Bank queue | 최대 4 accepted beats/bank; DMA/IME task round-robin |
| DMA | 전체 16 packet/staging credits; SID 기본 `0x100` |
| Packet | `min(remaining, 64B boundary, 4KiB boundary)` |
| DMA issue 제한 | packet당 `ceil(bytes/16)` NPU cycles; 단일 외부 RequestPort |
| IME | engine당 8×8 output tile, 64개의 uint32 partial sum |
| Operand buffer | 현재 K step의 최대 A 8 B + B 8 B/engine |

SRAM의 DMA/IME read/write는 모두 bank queue를 통과한다. Read 응답은 SRAM
write 완료까지 DMA credit을 유지하고, STORE는 SRAM read 완료 후 발행한다.
Task는 다음 bank를 수락하지 못하면 그 task의 뒤쪽 beat도 기다린다. 따라서
**task 내부 head-of-line 대기**가 있으며 bank 폭만으로 처리량을 계산하면 안 된다.
`bank_queue_depth`는 큐 용량이고 같은 cycle의 여러 beat 수락 능력이 아니다.

IME는 K step마다 A row/B column operand read 완료를 기다리고, 한 cycle의
MAC 후 다음 K operand를 요청한다. K step 간 operand prefetch pipeline은 없다.
Partial sum은 내부 buffer에 유지하고 최종 INT32 결과는 SRAM bank write로
반영한다. Tail tile은 유효 row/column만 계산한다. 출력과 입력의 SRAM overlap은
기존 ABI처럼 거절하며, A/B 간 alias는 허용한다. K의 기존 제한과 uint32
accumulation으로 host signed-overflow UB에 결과를 맡기지 않는다.

따라서 engine당 64 MAC/cycle은 **operand가 준비된 arithmetic step의 최대치**다.
SRAM 왕복/경합과 tile 출력 저장을 포함한 sustained 성능이나 실제 NPU 성능이
아니다. DMA issue 제한 역시 선언한 공급률 envelope이며 실물 버스의 header,
credit, independent read-response/write-data lane을 재현하는 모델은 아니다.

## opt-in 비동기 DMA 확장

`enable_async_dma=True`일 때만 새 package variant
`FUNC_OPS_ASYNC=4`를 실행한다. `required_features`에는 `ASYNC_DMA=1<<2`가
반드시 있어야 하며 DMA/IME의 기존 bit 0/1과 조합한다. 이때 CAP0은 `0x602`,
CAP1은 `0x302`다. 기존 variant 1과 DMA op 1/2의 직렬 의미는 유지한다.
기존 reserved opcode 4–7이나 reserved field를 조용히 재해석하지 않는다.

| Opcode | 필드 |
|---|---|
| `0x100 DMA_LOAD_ASYNC` | dst=SRAM, src0=binding, src1=binding offset, arg0=bytes, arg1=R0(0)/R1(1), arg2=token(0..7) |
| `0x101 DMA_STORE_ASYNC` | dst=binding, src0=SRAM, src1=binding offset, arg0=bytes, arg1=W0(2), arg2=token(0..7) |
| `0x102 WAIT` | arg0=nonzero token mask(1..255), 나머지 모든 field=0 |

모든 op의 flags와 arg3는 0이다. 한 channel은 descriptor 하나를 활성 상태로
유지하며 세 channel은 공유 16 packet credits를 round-robin으로 배분한다.
Channel이 차 있으면 제어 엔진은 해당 op에서 기다린다. 무제한 descriptor
queue나 channel별 16 packet을 만드는 모델이 아니다.

Token은 issue 후 예약되며 LOAD는 SRAM 반영까지, STORE는 모든 외부 write
응답까지 완료된 뒤 done이 된다. WAIT는 지정한 모든 token의 done을 기다리고
해당 token을 해제한다. 완료되었더라도 WAIT 전 token 재사용은 오류다.
발행되지 않은 token의 WAIT, 잘못된 channel, 범위 밖 token은 preflight에서
거절한다. END는 남은 DMA를 모두 기다린다.

SRAM scoreboard는 pending LOAD/write와 read/write, pending STORE/read와
write의 주소 충돌을 기다린다. Read/read는 허용한다. MAC은 A/B read와 C write
범위의 충돌이 해소되어야 시작한다. 이 제어 엔진은 MAC 실행 중 다음 op를
발행하지 않으므로, overlap하려는 DMA는 MAC보다 먼저 issue해야 한다.
Double buffering은 서로 겹치지 않는 A0/A1, B0/B1, C0/C1 영역을 package가
선택한다. SRAM bank 경합은 영역이 분리되어 있어도 발생할 수 있다.

## 오류, RESET 및 완료 경계

- `sendTimingReq=false`이면 동일 packet을 retry까지 보유한다.
- Caller-owned `SMMUv3Fault` sender state로 permission fault `0x13`을 NPU
  FAULT 8에, 나머지 DMA 오류를 FAULT 7에 연결한다. 실패한 read를 SRAM에 쓰거나
  실패를 정상 MAC/DONE으로 진행하지 않는다.
- 오류/ABORT/RESET은 새 발행을 멈추고 unaccepted packet을 취소한다.
  Accepted packet 및 이미 수락된 SRAM beat는 drain 후 상태를 재사용한다.
- 이 drain 동안 STATUS.BUSY는 유지된다. BUSY가 사라지기 전에 IOVA/buffer를
  해제하거나 다음 job을 시작하면 안 된다. 이미 수락된 write를 되돌리지는 않는다.
- RESET은 drain 뒤 `2MiB/(16×4B)=32768` NPU cycles의 clear 시간을 추가하고
  SRAM/register/per-job counter를 초기화한다. 이는 실물 reset 시간 자료가 아니다.
- DONE은 모든 op와 DMA/SRAM/MAC 작업의 모델상 완료다. STORE 응답이나 DONE을
  DRAM burst 전송 완료로 해석하지 않는다. Native MemCtrl drain은 별도 경계다.
- Checkpoint는 native drain 후 idle SRAM/register 상태만 저장한다.

## 통계와 해석

| 통계 | 의미 |
|---|---|
| `PERF_DMA_BYTES` | 성공한 tensor DMA 응답 bytes; metadata 제외 |
| `PERF_IME_OPS`, `macs` | MAC 수; 산술 OPS 환산은 `2×MAC` |
| `PERF_CYCLES` | 실행 중/완료 job의 NPU elapsed cycles |
| `PERF_STALLS` | 기존 ABI의 fault 횟수; pipeline stall cycles가 아님 |
| `firstPayloadAcceptedTick`, `lastPayloadResponseTick` | stats reset 이후 최초 payload 수락 / 마지막 payload 응답의 절대 simulation tick; metadata 제외 |
| `dmaRequests`, `dmaRetries` | 수락한 packet 수 / 거절된 request 시도 수 |
| `dmaReadBytes`, `dmaWriteBytes` | 정상 실행 경로에서 처리한 성공 payload 응답 bytes; stop/drain 중 응답 제외 |
| `metadataReadBytes`, `dmaFailedBytes` | 정상 실행 경로의 성공 metadata / 오류 응답 bytes |
| `sramReadBytes`, `sramWriteBytes` | 완료된 논리 byte 접근; reset clear 제외 |
| `sramBankBeats`, `sramBankStalls` | 수락 beat / bank arbitration 차단 시도 |
| `outstandingHighWater` | packet+SRAM staging entry 최대 점유, 상한 16 |
| `dmaChannelsHighWater` | 동시에 활성화한 async descriptor channel 수 |
| `dmaChannelsInFlightHighWater` | 수락 후 응답을 기다리는 packet이 있는 channel 수 |
| `dmaMacOverlapCycles` | DMA entry가 있고 IME tile 실행 단계가 활성인 cycles |
| `dependencyStalls` | token/channel/SRAM dependency 때문에 제어가 기다린 cycles |

`dmaMacOverlapCycles`에는 IME operand/SRAM 대기도 포함된다. 정확히 같은
cycle에 산술 연산과 DRAM burst가 실행되었다는 counter가 아니다.
`firstPayloadAcceptedTick`은 아직 payload 수락이 없으면 0이다.
`lastPayloadResponseTick`에는 오류 응답도 포함하므로 성공/정답 검사와 함께
사용한다. `dmaReadBytes`, `dmaWriteBytes`, `metadataReadBytes` 및
`PERF_DMA_BYTES`는 ABORT/RESET/fault로 stop/drain에 진입한 뒤 도착하는 성공
응답을 집계하지 않는다. 따라서 이 byte counter는 오류 실행의 전체 외부 DMA
트래픽이 아니며, drain 응답도 포함하는 `lastPayloadResponseTick`과 범위가
다르다. `dmaFailedBytes`는 stop/drain 중 오류 응답도 포함한다. 정상 완료된
benchmark의 byte 집계에는 이 제외 조건이 적용되지 않는다. Stats reset을 inflight 도중 실행하면 최초 수락 경계가 사라질 수 있다.
두 tick의 구간 overlap은 전체 payload phase가 겹친다는 근거이며 cycle별 DRAM
burst 동시 실행을 증명하지 않는다.
`outstandingHighWater`를 평균 outstanding이나 Little's-law latency로 대체하면
안 된다. 현재는 bank별 occupancy/latency histogram과 PA별 DRAM attribution을
추가하지 않았다. 실물 보정 또는 원인 분리가 필요하면 같은 관측 경계에서 이를
계측해야 한다.

## 재현 가능한 native 검사

```sh
build/ARM/gem5.opt --outdir=/tmp/npu-direct \
  tests/zena_cmn/directed/npu_v01.py
build/ARM/gem5.opt --outdir=/tmp/npu-smmu \
  tests/zena_cmn/directed/npu_v01.py --smmu
build/ARM/gem5.opt --outdir=/tmp/npu-async \
  tests/zena_cmn/directed/npu_v01.py --async-dma
build/ARM/gem5.opt --outdir=/tmp/npu-async-smmu \
  tests/zena_cmn/directed/npu_v01.py --async-dma --smmu
```

Fixture는 PIO, exact DMA byte/guard, signed GEMM/tail/hart 분할, preflight,
retry/credit, inflight RESET/ABORT, token/dependency/3-channel overlap을 검사한다.
`--smmu`는 metadata identity mapping 외에 tensor의 비연속 PA page 변환,
read로 채운 RO TLB의 write permission fault, EVTQ SID/IOVA/reason 및 TLBI 후
복구까지 검사한다. IRQ latch는 검사하지만 실제 Linux handler/fence 전달의
증거는 Linux 통합 검사에서 별도로 얻는다.

2026-10-18 최종 relink 후 `gem5/build/ARM/gem5.opt`로 다음 검사를 다시 통과했다.
검사 binary SHA-256:
`146260d0620253bc3062db7871aece670b9910a86b5236a1ee42d8906e1a0dfb`.
산출물 경로는 상위 workspace의 `build/gem5-zena/` 기준이다.

| 실행 | 통과 case | 산출물 |
|---|---:|---|
| Direct, 직렬 | 24 | `npu-production-direct/npu-result.json` |
| SMMU, 직렬 | 29 | `npu-production-smmu/npu-result.json` |
| Direct, 비동기 | 35 | `npu-production-async/npu-result.json` |
| SMMU, 비동기 | 40 | `npu-production-async-smmu/npu-result.json` |

합계 128개 case이며 네 process 모두 exit 0이었다. Doorbell 우선순위와
`RESET|ENABLE` 유지, metadata-only 실행의 payload timestamp 0 및 payload 실행의
시작/종료 순서를 포함한다. 비동기 두 실행에서 accepted packet 기준 동시 channel
최댓값은 3, packet/staging credit 최댓값은 16이었다. DMA/IME 활성 구간 overlap은
각각 2997/3002 cycles이며 위 통계 해석의 제한을 적용한다.

이 결과는 native device/SMMU 모델 검사다. Linux handler/fence, CHI cache
maintenance 및 동시 CPU/NPU workload 검사는 상위 통합 보고서에서 별도로
관리한다. 실물 NPU/SMMU 성능 보정, 실제 RV32/RVV firmware 실행, 전력 모델은
이번 모델의 검증 범위에 포함하지 않는다.
