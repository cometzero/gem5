# 모델과 검증 경계

| 영역 | 제공 | 제공하지 않는 것 |
|---|---|---|
| CPU | 현재 gem5 generic ArmO3 및 native FU pool, 명시적 큐 설정 | A720AE 내부 구조 보정, 실제 칩 IPC 보증 |
| Coherency | native CHI cache controllers, RN-F→DSU→Home, DMT/DCT, directory-only 대조 | CMN 제품의 정확한 microarchitecture 및 register map |
| Network | Garnet mesh/local-router topology, VC/flit/bank/TBE 제한 | 실제 인터커넥트 credit/latency calibration |
| Memory | 기존 LPDDR5X class/native MemCtrl, 같은 총 용량 4/8/16채널 | 실물 LPDDR5X 부품·controller 측정 보정 |
| Linux | 기존 AP firmware/DT/MMIO/IRQ, atomic_noncaching→O3 | 다른 domain firmware와 full-SoC 동작 |
| Topology 노출 | DT의 private L2/shared L3와 실제 Ruby 경로, opt-in CLIDR/CCSIDR geometry | 실물 cache hierarchy 보정 |
| DMA | block device의 CHI RNI 연결, NPU v0.1 SRAM/MAC/DMA 및 native SMMUv3 | 실물 NPU 성능 보정, 임의 NC master의 coherence 보증 |
| ROI | 게스트 marker, reset/dump 경계, tick 기반 통계 선택 | 자동 dirty-cache DRAM drain, 물리주소별 kernel traffic 분리 |
| Statistics | native counters, unavailable/ambiguous 구분 | TBE 중복 이름을 request occupancy로 추정하는 처리 |
| 검증 | 구성 checker, native RubyTester, Linux guest smoke, host workload tests | 이들 중 한 종류의 PASS로 나머지 증거를 대체하는 해석 |

실행별 성공/실패와 현재 남은 문제는 [implementation_report.md](implementation_report.md)를 따른다. 체크리스트 전체 완료나 실제 hardware qualification을 주장하지 않는다.

NPU의 지원 범위와 실행 방법은 [npu_model.md](npu_model.md) 및 [npu_quick_start_ko.md](npu_quick_start_ko.md)를 따른다. Linux NPU 실행에는 workspace의 커널 드라이버, `npu_apps`, `scripts/build_gem5_zena_linux.sh --profile npu`가 별도로 필요하며 gem5 체크아웃만으로 재현되지 않는다.
