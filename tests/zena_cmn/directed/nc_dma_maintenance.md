# CHI 비일관성 DMA 가시성 검증

`nc_dma_maintenance.py`는 CPU sequencer와 CHI를 우회하는 외부 master를
**동일한 native DRAM controller**에 연결한다. `ChiDmaTest`가 각 요청의 timing
응답을 기다리며 8-byte 값과 완료 순서를 검사한다. functional 메모리 복사나
별도 shadow memory로 결과를 맞추지 않는다.

```sh
build/ARM/gem5.opt --outdir=/tmp/chi-nc-sequential \
  tests/zena_cmn/directed/nc_dma_maintenance.py \
  --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml
build/ARM/gem5.opt --outdir=/tmp/chi-nc-parallel \
  tests/zena_cmn/directed/nc_dma_maintenance.py \
  --profile configs/zena_cmn/profiles/a720ae_npu_v01.yaml --parallel
```

검증 항목:

- CPU dirty write 뒤 clean 전에는 외부 read가 이전 DRAM 값을 읽는 음성 대조.
- clean 완료 이후 외부 read가 CPU의 새 값을 확인.
- 외부 write 뒤 invalidate 전에는 CPU가 이전 cache 값을 읽는 음성 대조.
- invalidate 이후 두 CPU의 read가 새 DRAM 값을 확인.
- IVAC가 stale dirty 값을 DRAM으로 writeback하지 않는지 확인.
- cacheable alias를 정리한 뒤 NC write/read의 비할당성과 다른 8-byte 영역 보존.
- 같은 line의 연속 NC write 및 CMO 이후 외부 read의 순서.
- `--parallel`: 두 CPU의 동시 same-line CMO, CMO 뒤에 대기하는 same-line
  read, 32-line CMO/NC-write 경쟁. 작은 TBE 설정만으로 실제 retry 발생을
  단정하지 않으며 ProtocolTrace/통계에서 별도로 확인해야 한다.

구현 경계:

- `CacheClean`/`CacheInvalidate`는 gem5 CHI 프로토콜의 기능 확장이다.
  실제 CHI opcode를 그대로 재현한 것으로 해석하지 않는다.
- CVAC도 보수적으로 clean+invalidate한다. DMA 가시성 검증과 별개로,
  clean 이후 cache 보존에 따른 성능은 보수적으로 모델링된다.
- NC 접근 전에 cacheable alias의 clean/invalidate가 필요하다. 임의의 dirty
  alias를 가진 NC 접근의 자동 일관성을 제공하지 않는다.
- NPU profile의 SNF `wait_for_write_ack=true`는 memory controller의 write
  응답 이후 completion을 반환한다. 이는 DRAM burst의 물리적 완료가 아니다.
  기본값 false는 기존 CPU 전용 profile의 posted-write 동작을 유지한다.
- 위 테스트는 CPU 명령 실행/DSB 전체 검증이 아니다. Linux DMA API의
  실제 cache instruction 경로와 NPU 재제출은 full-system 테스트로 검증한다.

결과는 실행 디렉터리의 `nc-dma-result.json`에만 기록되며 실행 시작 시 이전
결과를 삭제한다. 소스에 PASS를 미리 기록하지 않는다.

2026-10-18 실행 증거는 workspace의
`build/gem5-zena/npu-chi-maintenance/summary.json`에 모았다.
7개 실행, 총 2,262개 요청이 PASS했고 모두 drain을 완료했다.
같은 DSU의 병렬 시험은 RetryAck/PCrdGrant/DoRetry 각각 156회,
서로 다른 DSU의 시험은 각각 152회를 실제 trace에서 확인했다.
`--cross-cluster`는 peer를 CPU1 대신 CPU4에 연결한다.

| 시험 | 요청 수 | 첫 clean 진입 시 dirty 데이터의 위치 |
|---|---:|---|
| 기본 / 같은 DSU 병렬 | 248 / 451 | L1D |
| 다른 DSU 기본 / 병렬 | 248 / 451 | L1D |
| `--evict-depth 1` | 272 | L1=I, L2=UD |
| `--evict-depth 2` | 288 | L1=I, L2=I, DSU L3=UD |
| `--evict-depth 3` | 304 | L1=I, L2=I, DSU L3=I, HNF SLC=UD |

위 위치는 주소 `0x100000`의 첫 CacheMaintenance transition과 config.ini의
controller version을 대조했다. clean이 진행되면 dirty 데이터가 아래 계층으로
이동하므로, 뒤에 관찰되는 UD를 최초 위치로 잘못 해석하지 않는다.
작은 cache geometry와 prefetch off를 사용한 기능 시험이며 성능 측정은 아니다.
