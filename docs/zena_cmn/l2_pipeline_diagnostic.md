# Private L2 read-hit pipeline 진단

`cache.l2.read_hit_pipeline_cycles`는 native CHI `read_hit_latency`를
private L2에만 연결한다. 기본값은0이며 기존/default/public candidate는
동작을 유지한다. `a720ae_l2_latency_diagnostic.yaml`만1을 지정한다.

`CHI-cache-actions.sm::ReadHitPipe`는 transaction의 `delayNextAction`에
지연을 추가한다. RubyCache data array의 `dataAccessLatency`를 늘리지
않으므로 BankedArray service interval 설정을 보존한다. 그러나 TBE가
더 오래 점유되어 동시 요청 처리량에 간접 영향은 있을 수 있다.

새 profile은 기존 integrated diagnostic을 복사해 L2 hit pipeline만
1cycle 추가한다. 기존 pointer endpoint 약8cycle을 목표9cycle로 맞추기
위한 **endpoint fitting**이다. 같은 pointer workload에서9cycle을 얻어도
독립 성능 검증은 아니다. load-streams 대조는 별도 workload의 처리량 및
queue 영향 확인에 쓰며, 실물 validation을 대체하지 않는다.

Simple network와 private-cache zero handoff를 포함하는 기존 integrated
profile의 근사 한계는 유지된다. 이 실험으로 전체 CMN transport를
하드웨어에 맞췄다고 주장하지 않는다.

Schema는 이 새 필드에만0을 허용하며 음수·bool·실수·문자열·31bit 초과를
거부한다. 기존 array latency는 양수 조건을 유지한다. Runtime verifier는
실제 L2 controller의 값과 profile을 비교하며 mutation 검증을 포함한다.

## 실행 결과

`build/gem5-zena/calibration-l2-latency` Linux calibration은8/8 PASS,
host297.28s였다. native load-to-use 통계는 L1=4.000488cycle,
L2=9.006099cycle, L3=23.483934cycle이다. L1 LDR Q 처리량은89.1040GB/s이다.
이전 integrated 결과의 L2=8.005855cycle보다 약1cycle 증가했다.
단, 이전 calibration guest binary와 최근 workload 확장 후 binary의 hash가
달라 완전한 binary-identical A/B로 취급하지 않는다.

이를 분리하기 위해 현재 guest binary로
`build/gem5-zena/calibration-l2-latency-control`을 추가 실행했다.
control도8/8 PASS(host292.90s)이며 guest/gem5/kernel/bootloader hash가
pipeline1 런과 모두 일치한다. 이 대조에서 L2는 **8.006343→9.006099cycle**,
L1=4.000488cycle, L3=23.483934cycle, L1 LDR Q=89.1040GB/s는 정확히 같았다.
따라서 동일 실행 파일 조건에서도 L2 hit 지연 증가를 확인했다.

`build/gem5-zena/load-streams-l2-latency`는15/15 PASS, host350.57s였다.
이 대조는 기존 `load-streams-integrated`와 guest/gem5/kernel/bootloader
SHA256이 모두 같다.

| L2 독립 stream 수 | 기존 GB/s | pipeline1 GB/s |
|---:|---:|---:|
| 1 | 2.29259 | 2.29645 |
| 2 | 3.53370 | 3.53370 |
| 4 | 3.98492 | 3.98395 |
| 8 | 3.98686 | 3.98541 |
| 16 | 3.99415 | 3.99366 |

고동시성 처리량은 거의 같았다. scalar line당8B 사용과 array bank 접근
패턴이 포함된 결과이며 L2 전체 bytes/cycle peak 측정은 아니다.
L2 hit fraction은99.95% 이상이다. 두 run의 native config mutation 검사도
각13건 PASS했고 schema는14건 PASS했다.

## 설정 선택과 Garnet를 유지하는 후속 범위

현재 하드웨어 유사성 실험의 우선 설정은 **Garnet 기반 public candidate를
유지하고 private cache transport 지연 오차를 명시하는 것**이다. 위 Simple
profile은 local pipeline fitting 진단으로만 선택한다. Simple 전환은 local
L1↔L2뿐 아니라 전체 mesh의 전달·중재·buffer 및 flit/credit 표현도 바꾸므로,
9cycle endpoint를 얻었다고 CMN 하드웨어 유사성이 개선되었다고 단정할 수 없다.

기존 파라미터만으로 private bypass를 구성할 수 있는지는 다음 소스로 확인했다.

- `configs/ruby/CHI_config.py::CHI_Node.connectController()`는 모든 controller의
  req/rsp/snp/dat를 같은 `ruby.network`에 연결한다.
- `configs/topologies/CustomMesh.py::_createRNFRouter()`는 RNF 내부 controller를
  별도 router에 연결한다. `makeTopology()`는 Garnet local router latency를1,
  Simple은0으로 지정한다.
- `src/mem/ruby/network/garnet/NetworkInterface.cc`의 `flitisizeMessage()`,
  `wakeup()` 및 output link scheduling에는 VC/credit와 cycle 경계가 있다.
  같은 router 배치나 작은 link latency만으로 NI/router 자체를 우회하지 않는다.

후속 구현의 좁은 범위는 같은 코어 L1I/L1D↔private L2 목적지를 식별하는
**local message bridge**이다. 이 경로만 전달하고 다른 목적지는 기존 Garnet로
보내야 한다. L2↔DSU 및 Home/SNF/remote traffic은 Garnet에 남긴다.
단순 MessageBuffer 직결은 복수 목적지와 snoop/data 반환을 처리하지 못한다.

추가 모델은 네 CHI vnet, 유한 queue/서비스 대역폭/backpressure, local/remote
혼합 목적지와 multicast, ordering, snoop·writeback·exclusive eviction,
drain/checkpoint를 다뤄야 한다. local latency/처리량과 remote flit/credit 통계가
서로 분리되는지도 검증해야 한다. 이는 Python latency 설정만의 변경이 아니라
Ruby network adapter/NI와 topology wiring을 포함하는 별도 구현 과제이다.
현재 작업에서는 해당 core/network 코드를 변경하지 않았다.
