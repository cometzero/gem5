# 비일관성 NPU DMA를 위한 CHI cache maintenance 검증

2026-10-18 현재 7개 native 실행에서 총 **2,262개 timing 요청이 PASS**했다.
각 실행은 마지막 응답 이후 `m5.drain()`까지 완료했다. Linux의 cache instruction
실행과 NPU driver ownership 전환은 별도 full-system 검증 대상이다.

## 재현 명령

workspace의 `gem5/`에서 실행한다. 출력 경로는 workspace의
`build/gem5-zena/npu-chi-maintenance/`다. 기본 profile의 전체 CPU/DSU/HNF/DRAM
연결을 사용하되, 기능 시험의 request TBE를 2개로 줄이고 L2 prefetch를 끈다.
`--evict-depth` 실행은 아래에 설명한 작은 cache geometry를 추가 적용한다.

```sh
build/ARM/gem5.opt --outdir=../build/gem5-zena/npu-chi-maintenance/sequential tests/zena_cmn/directed/nc_dma_maintenance.py --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml
build/ARM/gem5.opt --outdir=../build/gem5-zena/npu-chi-maintenance/parallel --debug-flags=ProtocolTrace --debug-file=protocol.log tests/zena_cmn/directed/nc_dma_maintenance.py --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml --parallel
build/ARM/gem5.opt --outdir=../build/gem5-zena/npu-chi-maintenance/cross-sequential --debug-flags=ProtocolTrace --debug-file=protocol.log tests/zena_cmn/directed/nc_dma_maintenance.py --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml --cross-cluster
build/ARM/gem5.opt --outdir=../build/gem5-zena/npu-chi-maintenance/cross-parallel --debug-flags=ProtocolTrace --debug-file=protocol.log tests/zena_cmn/directed/nc_dma_maintenance.py --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml --parallel --cross-cluster
build/ARM/gem5.opt --outdir=../build/gem5-zena/npu-chi-maintenance/depth1 --debug-flags=ProtocolTrace --debug-file=protocol.log tests/zena_cmn/directed/nc_dma_maintenance.py --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml --evict-depth 1
build/ARM/gem5.opt --outdir=../build/gem5-zena/npu-chi-maintenance/depth2 --debug-flags=ProtocolTrace --debug-file=protocol.log tests/zena_cmn/directed/nc_dma_maintenance.py --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml --evict-depth 2
build/ARM/gem5.opt --outdir=../build/gem5-zena/npu-chi-maintenance/depth3 --debug-flags=ProtocolTrace --debug-file=protocol.log tests/zena_cmn/directed/nc_dma_maintenance.py --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml --evict-depth 3
```

실제 실행은 host `timeout` 120초 또는 150초로 감쌌다. 결과는 각 디렉터리의
`nc-dma-result.json`, 설정은 `config.ini`, 로그는 `run.log`와 `protocol.log`다.
전체 집계와 설정/로그 hash는 `../build/gem5-zena/npu-chi-maintenance/summary.json`에
있다. 결과 JSON은 시험 시작 시 삭제하므로 실패한 재실행이 이전 PASS를 재사용하지 않는다.

## 관측 결과

| 실행 | 요청 | 종료 tick | drain | 결과 |
|---|---:|---:|---|---|
| sequential | 248 | 12,132,438 | 완료 | PASS |
| parallel | 451 | 20,465,148 | 완료 | PASS |
| cross-sequential | 248 | 12,327,438 | 완료 | PASS |
| cross-parallel | 451 | 20,328,648 | 완료 | PASS |
| depth1 | 272 | 14,681,938 | 완료 | PASS |
| depth2 | 288 | 16,230,438 | 완료 | PASS |
| depth3 | 304 | 18,114,938 | 완료 | PASS |

기본 peer는 CPU1, `--cross-cluster` peer는 다른 DSU에 연결된 CPU4다.
CPU dirty write 뒤 clean 전의 외부 read가 **이전 DRAM 값**을 읽고,
외부 write 뒤 invalidate 전의 CPU read가 **이전 cache 값**을 읽는 두 음성 대조를
포함한다. clean 이후 외부 read, invalidate 이후 두 CPU의 read, stale dirty
IVAC의 writeback 방지, NC partial write의 주변 byte 보존도 확인했다.

병렬 시험은 peer 두 개의 같은 line CMO, CMO 뒤에 대기하는 같은 sequencer의
read, 32-line clean/NC write 경쟁을 실행했다. 실제 ProtocolTrace의 수신/재시도
transition 수는 다음과 같다. 작은 TBE 설정만으로 retry 발생을 추정하지 않았다.

| 실행 | RetryAck | PCrdGrant | DoRetry |
|---|---:|---:|---:|
| parallel | 156 | 156 | 156 |
| cross-parallel | 152 | 152 | 152 |

### Dirty 데이터 위치 확인

Eviction 시험에서는 L1D 1KiB/2-way, L2 4KiB/2-way, DSU L3 16KiB/2-way,
HNF SLC 64KiB/2-way를 사용한다. stride는 depth별 512/2048/8192 B,
경쟁 line 수는 `2 * depth + 1`이다. 이것은 기능 시험용 설정이다.

주소 `0x100000`의 첫 `CacheMaintenance` transition과 `config.ini`의 controller
version을 대조했다. version 1=L1D, 8=L2, 12=DSU L3, 52=HNF0 SLC다.

| 실행 | 첫 CMO의 L1D 상태 | L2 상태 | DSU L3 상태 | 최초 dirty 위치를 확인한 tick |
|---|---|---|---|---:|
| parallel | UD | 이후 copyback 수신 | 이후 copyback 수신 | L1D @ 134,500 |
| depth1 | I | UD | 이후 copyback 수신 | L2 @ 481,000 |
| depth2 | I | I | UD | DSU L3 @ 687,000 |
| depth3 | I | I | I | HNF SLC UD @ 912,000 |

따라서 요청한 eviction depth뿐 아니라 **실제 첫 clean의 dirty 시작 위치**를
확인했다. clean 진행 중 아래 계층이 copyback을 받아 UD로 바뀌는 기록은
최초 위치와 구분한다. 각 계층의 성능이나 실제 하드웨어 cache 유지 정책을
이 작은 geometry 결과로 예측하지 않는다.

## 구현과 해석의 경계

- CPU의 CMO는 각 계층의 timing snoop/copyback을 통해 HNF/SNF까지 진행한다.
  host의 functional flush나 shadow memory 복사로 DMA 결과를 보정하지 않는다.
- `CacheClean`/`CacheInvalidate`는 gem5 CHI의 기능 확장이다. 실제 CHI opcode를
  그대로 구현했다고 주장하지 않는다. CVAC도 보수적으로 clean+invalidate하므로,
  clean 이후 cache 보존에 따른 성능은 보수적이다.
- NPU profile은 모든 SNF에서 `wait_for_write_ack=true`를 사용한다. write TBE를
  native memory response까지 유지해 같은 line의 이전 ACK가 다음 write를
  완료시키는 오류를 방지한다. 이는 **controller 응답/수락** 경계이며 실제 DRAM
  write burst 완료를 뜻하지 않는다. 기본값 false는 기존 CPU-only profile의
  posted-write 동작을 유지하며 외부 DMA ownership barrier로 검증한 설정이 아니다.
- CPU NC metadata는 nonallocating 요청으로 전달하고 store completion을 downstream
  completion 이후에 반환한다. NC 접근 전 cacheable alias의 clean/invalidate가
  필요하며 임의의 dirty alias를 자동으로 일관되게 만드는 기능은 제공하지 않는다.
- native fixture는 CPU 명령 실행/DSB 전체를 검증하지 않는다. Linux DMA API와
  architectural cache ID, IRQ, fence, SMMU stage-1, 반복 BO 사용은 full-system
  실행으로 별도 확인한다. 실제 silicon 대역폭이나 보정 완료를 뜻하지 않는다.
