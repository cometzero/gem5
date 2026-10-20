# Zena CPU–DSU–CMN–LPDDR5X 구현·검증 보고서

작성·검증: 2026-10-13~14. gem5 checkout HEAD: `4d869b97a749` + 기존 LPDDR5X 변경. 판정 범위: **UNCALIBRATED_PROXY / MODEL_ONLY**.

## 적용한 범위와 설계 조정

`doc/gem5`의 설계 입력을 바탕으로 기존 Classic과 독립적인 Ruby CHI full-system 경로를 구현했다. 사용자의 후속 지시에 따라 문서의 single-protocol 조건을 강제하지 않고, 기존 `build/ARM`의 `USE_MULTIPLE_PROTOCOLS=y`, `PROTOCOL="CHI"`를 유지했다. 실제 CHI 트랜잭션 테스트로 호환성을 확인했다. 별도 `ARM_CHI` 빌드는 중단했으며 필수 산출물로 사용하지 않는다.

기존 Classic 설정, 제거된 DRAM tuning/계측/WB-NC 예약 매핑은 다시 도입하지 않았다. 신규 경로의 큐/캐시 설정은 이번 설계 입력의 초기값이며 실물 성능 보정값이 아니다. gem5 `src/`의 C++/SLICC/DRAM 구현은 변경하지 않았다.

## 실제 구성

| 항목 | 구현 |
|---|---|
| CPU | generic ArmO3 16개, 4코어 × 4 cluster, 2 GHz |
| CPU 큐 | ROB192 / native IQUnit128 / LQ32 / SQ64; 기존 FU pool 유지 |
| L1 | 코어마다 I/D 각 64 KiB, 4-way |
| L2 | 코어마다 512 KiB, 8-way, 총 16개 |
| DSU L3 | cluster마다 4 MiB, 16-way, non-home CHI intermediate cache |
| Home/SLC | 글로벌 Home 8개, 각 1 MiB, 16-way |
| 연결 | L1→private L2→cluster DSU L3→Home→SNF→native MemCtrl |
| Network | 4×4 mesh + 4개 cluster local router; local마다 양방향 mesh edge 하나 |
| Garnet | 32 B flit, 4 VC/VN, table routing; `num_rows=0`은 추가 local router를 포함하는 그래프 표현용 |
| DRAM | 기존 LPDDR5X-8533 x16 class 그대로 재사용, 4/8/16 channel, 총 2 GiB 고정 |
| Home 선택 | `(PA >> 6) & 7` |
| 채널 선택 | `(PA >> 6) & (channels - 1)`, XOR 없음 |
| 주소 | RAM `0x80000000..0xffffffff`, 기존 firmware 2 MiB 예약 유지 |
| 부팅 | 기존 Zena PSCI firmware·Linux·initramfs·PL011·GICv3·timer 재사용 |
| I/O | 기존 TimingVirtIOBlock의 DMA를 CHI RNI에 연결; base disk는 Cow 보호 |
| NPU | 미구현. 별도 coherent NPU 포트나 가상의 중재 hop을 만들지 않음 |

실물 DSU/CMN을 레지스터 수준으로 재현하거나 Cortex-A720AE의 내부 구현을 모사한다는 의미가 아니다. `access_backing_store=False`이며 별도 shadow store로 정합성을 우회하지 않는다.

## 실행 및 계측

`configs/zena_cmn/run_fs.py`가 기존 플랫폼의 이미지 검증 및 DT 생성 기능을 재사용하고, private L2/shared L3 DT 연결을 추가한다. YAML에 없는 키/잘못된 타입/불일치 topology는 거절한다. CLI에서 지정한 값만 YAML을 덮어쓴다. 부팅 asset은 CLI가 소유하고 실제 경로와 SHA256을 manifest에 기록한다.

게스트 marker 순서는 `0x5a535743`(전체 CPU O3 전환) → 준비·warmup → workbegin 0(ROI reset) → workend 0(ROI dump) → 정답 검사다. native `switchCpus`가 drain과 memory mode 전환을 수행한다. 중복/순서 오류 marker는 실패한다. checkpoint restore는 제공하지 않는다.

`run_smoke.py`는 initramfs를 복사·확장하고 원본을 수정하지 않는다. UART, 명령, 입력 hash, 종료 상태, `config.json`, `roi-events.json`, `stats.txt`와 파싱 결과를 남긴다. ROI 경계의 실제 tick으로 통계 블록을 선택하므로 warmup dump나 종료 시 자동 dump를 합산하지 않는다.

통계는 App useful bytes와 native DRAM burst accounting을 구분한다. IRQ/kernel/page-walk 트래픽은 DRAM 값에 포함된다. 쓰기 통계는 dirty cache 전체의 DRAM 완료 보증이 아니다. CHI의 `avg_size/avg_util/avg_reserved`는 서로 다른 TBE pool에서 이름이 중복되므로 null과 ambiguity 목록으로 보존한다.

## 확보된 검증 증거

| 검증 | 결과 | 증거 |
|---|---|---|
| 기존 ARM build 증분 빌드 | PASS | `build/gem5-zena/cmn-baseline/build-existing.log` |
| YAML 검사 | 8개 PASS | `build/gem5-cmn/parameter-tests/schema.log` |
| 실제 AddrRange·DRAM 회귀·CPU 큐 | 7개 PASS | `build/gem5-cmn/parameter-tests/native-lq32.log` |
| runtime config/DT 검증기 | 7개 PASS | `tests/zena_cmn/test_verify_config.py` |
| 16 CPU 구성/실제 DT | PASS | `build/gem5-zena/cmn-fs-first/config-verification.json` |
| guest static AArch64 build | PASS | `workloads/zena_cmn/build.sh`, 실행별 guest-build.log |
| host guest 기능 self-check | 40개 PASS | `build/gem5-cmn/guest-host/self-check.json`; Linux/gem5 증거와 구분 |
| ROI lifecycle/parser | PASS | `tests/zena_cmn/test_roi_control.py`, `test_roi.py` |

Native RubyTester 실행 결과:

| cluster / channel | 검사 수 | 결과 | simulated ticks |
|---|---:|---|---:|
| 1 / 4 | 100 | PASS | 2,121,001 |
| 4 / 4 | 1,000 | PASS | 7,337,001 |
| 4 / 8 | 1,000 | PASS | 7,259,501 |
| 4 / 16 | 1,000 | PASS | 7,506,001 |
| 2 / 16, tiny cache | 1,000 | PASS | 12,093,011 |
| 4 / 16, DMT+DCT | 1,000 | PASS | 8,101,501 |
| 4 / 16, directory-only SLC | 1,000 | PASS | 7,542,001 |
| 4 / 16, DMT only | 1,000 | PASS | 7,591,501 |
| 4 / 16, DCT only | 1,000 | PASS | 8,170,501 |

근거: `build/gem5-zena/cmn-directed-summary.json` 및 각 디렉터리의 `directed-result.json`, `config.json`, `stats.txt`. 각 DSU에 실제 demand access가 발생했다. RubyTester는 data-side traffic을 사용하므로 instruction-side Linux/LLSC/TLBI 검증을 대신하지 않는다. tiny fixture는 production cache 용량을 변경하지 않는다. 이 실행에서 tag-array stall 465회, data-array stall 111회, reqRdy stall 5회와 최종 progress를 확인했다.

## 부팅 검증에서 발견한 문제와 수정

설계 입력의 LQ64에서 4코어 디스크 검사 및 16코어 정상 종료 경로에 동일한 native O3 assertion이 발생했다. 게스트 정합성 ROI 자체는 통과했으나 해당 전체 실행은 FAIL이다.

정확히 같은 초기 실행을 debug trace로 재현했다. tick `47240054500`에 한 CPU의 Ruby sequencer가 같은 64 B line에 대한 작은 read 49개를 함께 완료했다. response queue가 1 ps 간격으로 전달하면서 CPU의 다음 500 ps 주기 전에 IEW writeback 슬롯 48개(`(forwardComSize 5 + 1) × wbWidth 8`)가 소진되었고, 49번째 응답에서 assertion이 발생했다. 디스크 PIO 자체의 결함으로 단정하지 않는다.

근거: `build/gem5-zena/cmn-disk-crash-trace2/diagnosis.json`, `crash.trace`. 이 trace는 원래 실행과 ROI·crash tick이 동일하다.

사용자 지시에 맞춰 LQ만 현재 native 기본값 **32**로 변경했다. IQ128/SQ64/ROB192와 writeback 버퍼는 유지하여 인위적으로 버퍼를 확대하지 않았다. 생성 시 LQ가 native writeback 총 슬롯보다 큰 설정을 거부한다. 이 검사는 알려진 overflow 조건을 차단하며 모든 임의의 큐 조합에 대한 안전성 증명은 아니다.

`doc/gem5/zena_cpu_cmn_v01.yaml`은 원래 설계 입력으로 LQ64를 보존하고, 실행용 `configs/zena_cmn/profiles/default.yaml`은 검증 결과를 반영한 LQ32를 사용한다. 두 파일은 이제 byte-for-byte 동일하지 않다.

## Linux full-system 실행 결과

| 실행 | 조건 | 판정 | Host 시간 |
|---|---|---|---:|
| `cmn-linux16` | 초기 LQ64, 16 CPU, 64 MiB | 16 online·O3 전환 PASS, warmup 중 예산 종료; ROI 미실행 | 1,203.263 s |
| `cmn-linux4-disk-03` | 초기 LQ64, 4 CPU, 1 MiB, 1 iteration | 정합성 ROI PASS, 이후 writeback assertion; 전체 FAIL | 207.674 s |
| `cmn-linux16-quick` | 초기 LQ64, 16 CPU, 1 MiB, 2 iterations | cross-cluster 정합성·LLSC·TLBI·ROI PASS, 종료 assertion; 전체 FAIL | 338.711 s |
| `cmn-linux4-native-lq-disk` | LQ32, 4 CPU/4 channel, 1 MiB, 2 iterations | 정합성·디스크·정상 종료 **PASS** | 417.106 s |
| `cmn-linux16-native-lq` | LQ32, 16 CPU/16 channel, 1 MiB, 2 iterations | 교차 cluster·LLSC·TLBI·ROI·정상 종료 **PASS** | 353.995 s |

4코어 최종 실행의 ROI는 324.130 µs, 16코어는 371.327 µs다. 16코어 실행은 `m5_exit` code 0과 PSCI SYSTEM_OFF까지 완료했다. 이 workload는 기능 검사이며 메모리 bandwidth benchmark가 아니다. 이후 timing mode에서 ext4 쓰기→sync→unmount→read-only 재마운트→cmp를 완료했다. 실제 block 요청 41개(read20/write12/flush9), DMA read 146,638 B/write 127,427 B, IRQ 41개를 확인했다. 기본 디스크 SHA256은 실행 전후 동일하다.

실행 파일은 `build/gem5-zena/<실행명>/result.json`, `roi_summary.json`, `guest_console.log`, `m5out/config.json`, `m5out/stats.txt`이다. 최초 link 중 실행 실패와 짧은 smoke로 교체하며 중단한 시도도 `cmn-linux4-disk`, `cmn-linux4-disk-02`에 보존했다.

4코어 LQ32 실행은 iteration 수도 1→2로 변경했으므로 엄밀한 단일 변수 성능 비교가 아니다. 16코어 quick/native-lq 대조는 kernel·firmware·base initramfs·guest binary SHA256 및 guest command가 동일함을 확인했다. 이 비교도 성능 향상이 아니라 기능 회귀 해소 확인을 위한 것이다.

## 알려진 제한

- 기본 Arm ISA의 CLIDR은 캐시 0개를 보고한다(`src/arch/arm/isa.cc`). Linux 16 online CPU와 DT/cache 객체 구조는 별도로 확인해야 하며, sysfs cache 공유 목록까지 검증됐다고 주장하지 않는다. 이 노출 문제를 숨기려고 MIDR/CLIDR이나 Linux를 임의 변경하지 않았다.
- generic ArmO3의 명령 분해/FU/큐 수명은 실제 A720AE로 보정되지 않았다. mesh/TBE/cache latency도 초기 가정이다.
- full-system large-WSS 전체 1/4/8/16 worker × 2/4/8 pass 수렴 sweep, direct-timing Linux 대조, 계층별 residency 및 backpressure 원인 분리는 별도 검증 대상이다.
- 250 GB/s에 맞추는 인위적인 성능 목표를 적용하지 않는다. 현재 functional PASS를 실물 bandwidth 증거로 사용하지 않는다.

실행 방법은 [README.md](README.md), 실제 적용된 cache/TBE/pipeline 값은 실행별 `config.json`과 `parameter_provenance.csv`를 참조한다.

## 설계 체크리스트에 대한 최종 판정

| ID | 판정 | 근거/제한 |
|---|---|---|
| A01 | 수정 적용 PASS | 기존 multi-protocol CHI 빌드 재사용 및 실제 실행 |
| A02–A08 | PASS | schema, native AddrRange/MC 회귀, instantiated config/DT, 4/8/16채널 RubyTester |
| A09–A11 | PASS (MODEL_ONLY) | 16코어 guest 동일/교차 cluster, reader/writer, LL/SC 최종값 |
| A12 | PASS (sampled-line function), 전체 실행 PARTIAL | 후속 Linux 64 MiB 전체 line checksum/ROI와 eviction 카운터 PASS; post-ROI 정상 종료 전 timeout. 재검증 보고서 참조 |
| A13 | PARTIAL | 실제 DSU 접근·cache counters 확인; 단계별 residency microbench 미실행 |
| A14 | PASS (directed) | DMT/DCT 각각 및 동시 활성화 정답/경로 확인; Linux 조합 sweep 미실행 |
| A15 | PARTIAL | tiny-cache array/queue stall과 progress; 모든 TBE/VC 원인 분리 미실행 |
| A16 | PARTIAL | Linux16 online + 실제 DT 4 cluster/privateL2/sharedL3; cache sysfs unavailable |
| A17 | PASS (MODEL_ONLY) | 4코어 timing disk DMA·IRQ·검증·종료, 16코어 console/timer/종료 |
| A18 | PASS (functional) | 16코어 동일 VA의 서로 다른 memfd page 재매핑 검사; timing-DVM 별도 미실행 |
| A19 | PARTIAL | Atomic noncaching→O3 PASS; direct-timing Linux 대조 미실행 |
| A20–A21 | PASS | 명시 ROI 경계, 실행 통계 파싱, 중복/누락 fixture 검사 |
| A22 | NOT_RUN (gem5 performance) | 고정 WSS workload 구현·host 40건 확인; gem5 대용량 수렴 sweep 미실행 |
| A23 | PASS (configuration regression) | 기존 LPDDR class/MC timing과 주소 native 비교; 성능 실측 보정은 미실행 |
| A24 | PASS (scope/configuration) | NPU 미구현, CPU hierarchy 밖 future shared-backend 계약만 기록 |
| A25 | PASS (reporting) | 기능/구성/host/미실행 구분, bandwidth 목표 강제 없음 |

## 실패 항목 후속 검증 (2026-10-14)

[재검증 보고서](revalidation_report.md)에 원래 실패와 동일 입력의 LQ32
4코어 디스크 회귀, 64 MiB 페이지 준비 대조, CLIDR/cacheinfo 진단을 기록했다.
최초 실행을 위한 [Quick Start Guide](quick_start.md)도 추가했다.
기존 실패 이력은 위 표에 그대로 보존한다. 동일 입력 4코어 디스크 회귀는
415.079초에 정상 종료까지 PASS였다. 16코어 64 MiB는 모든 cache line의
정답과 ROI를 완료했으나 post-ROI 종료 전 3600초 제한에 걸려 전체 FAIL이다.
cache sysfs는 원인을 재현했으며 UNSUPPORTED로 유지한다.
