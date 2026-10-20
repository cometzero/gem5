# NPU v0.1용 native SMMUv3 검증

NPU는 별도 IOMMU 구현을 사용하지 않는다. `SMMUv3`와
`SMMUv3DeviceInterface`에 연결하며, 이 검사는 NPU 연산기 없이 native SMMU의
4 KiB stage-1 경로를 검증한다.

```sh
build/ARM/gem5.opt --outdir=/tmp/zena-smmu \
    tests/zena_cmn/directed/smmu_v01.py
```

정상 완료 시 `smmu-result.json`의 최상위 상태가 `PASS`다. 검사 중 assertion,
packet 미완료, simulator abort가 발생하면 결과 파일을 생성하지 않는다.
새 실행은 빈 output directory를 사용한다.

## 수정 범위

- 실패한 timing write가 점유한 interface write-buffer credit을 반환한다.
  Atomic 접근은 처음부터 credit을 예약하지 않으므로 반환도 하지 않는다.
- TLB hit에서도 cached writable 권한을 확인한다. RO page를 먼저 읽어
  TLB를 채운 뒤 write하는 경로를 포함한다.
- EVTQ entry를 0으로 초기화하고 event ID, SID, SSID-valid, IOVA, R/W를
  기록한다. Queue write를 직렬화하고 memory 응답 뒤 PROD를 공개한다.
- EVTQ의 wrap bit와 overflow/ack bit를 유지한다. Full queue에서 새로운
  event를 버리고 overflow를 기록하며, DMA fault response는 정상 반환한다.
- EVTQ consume 또는 interrupt disable 후 wired IRQ를 해제한다.
  `eventqInterruptActive()`는 실제 `ArmInterruptPin::active()`를 읽는
  관측 API이며 queue에서 IRQ 상태를 추측하지 않는다.
- CMDQ_CONS는 command 실행 뒤 증가한다. Maintenance는 신규 request를
  잠시 막고 기존 translation 및 payload response를 기다린 후 실행한다.
  따라서 기존 walk의 뒤늦은 TLB fill이 invalidate를 무효화하지 않는다.
- CMD_SYNC는 앞선 작업의 완료와 polling CONS를 지원한다. MSI completion,
  stall/resume, PRI/ATS는 이번 검증 범위에 포함하지 않는다.
- Interface drain은 반환된 translation slot 수 대신 아직 살아 있는
  translation/payload process를 확인한다. Command doorbell event도 drain에
  포함한다. Native response queue와 memory의 drain은 각 객체가 담당한다.
- Timing request/response의 header/payload delay는 process 재개 전에 소비한다.
- Invalid STE/CD, input address size, 잘못된 CD granule/T0SZ는 fault로
  반환한다. 4 KiB를 가로지르는 packet은 unsupported transaction fault이며,
  NPU DMA가 page/line 경계에서 split하는 계약을 유지한다.

## NPU에 전달하는 fault 정보

`SMMUv3Fault : Packet::SenderState`는 **요청자가 소유**한다. NPU가 요청 전에
이 state를 push하고, SMMU는 fault 때 `type`, `iova`, `sid`, `write`를 채운다.
응답 이후 NPU가 pop/delete한다. SMMU는 generic DMA 요청에 새 state를
할당하지 않는다. Permission `0x13`과 translation `0x10`을 bad-address
response만 보고 추정하지 않도록 하기 위한 내부 계약이다.

## Directed 검사와 관측 경계

| 검사 | 확인 항목 |
|---|---|
| Disabled/STE bypass | 정상 write response, 실제 PA 데이터 |
| Nonidentity, noncontiguous pages | 연속 IOVA가 떨어진 PA에 쓰임; 인접 PA 오염 없음 |
| 12회 unmapped write | 4-beat write buffer보다 많은 반복 fault가 모두 응답 |
| Fault recovery | 반복 fault 뒤 정상 write 완료 |
| RO read → write | TLB를 채운 read 후 write도 permission fault, PA 변경 없음 |
| Unmap/remap 및 TLBI | 새 PA에만 write, 이전 PA 보존 |
| In-flight TLBI + CMD_SYNC | 기존 walk 진행 중 CONS 조기 완료 금지, 이후 stale mapping 없음 |
| Descriptor/input fault | Event ID, SID/IOVA/R/W 및 IRQ assertion/consume |
| 140회 fault/128-entry EVTQ | Overflow 후에도 모든 response 완료, queue acknowledge와 정상 write 복구 |
| In-flight global drain | 실제 PA write와 잔여 응답 완료 이후 drain 반환 |

Fixture는 native `PyTrafficGen`을 사용한다. Generator의 error packet은
`totalReads`에 포함될 수 있으므로 성공/실패 판정에 그 이름을 사용하지 않는다.
`numPackets == totalReads + totalWrites`로 응답 회수를 확인하고, fault는
EVTQ 내용과 PA 데이터 보존으로 판정한다. 이 카운터를 DMA 대역폭으로 해석하지 않는다.

IRQ 관측용으로 WFI 상태의 AArch64 CPU와 GicV2를 둔다. Linux를 부팅하지 않으며
GIC distributor는 비활성 상태다. SID는 `0x100`이고 generator 제약 때문에
valid SSID 0을 사용한다. 여러 SSID나 PASID/SVA 검증을 뜻하지 않는다.

## 제한

Maintenance barrier는 전체 interface에 적용하는 보수적인 구현이다. 실물
SMMU의 invalidate 처리량이나 parallel walk 성능을 보정한 모델이 아니다.
Stage 2/nested translation, ATS/PRI, fault stall/resume, MSI CMD_SYNC,
page-table fetch external-abort 주입, checkpoint 복원은 별도 검증 대상이다.
Native SMMU ID register가 광고하는 기능과 이 문서의 검증 범위는 구별한다.

## Linux non-coherent DMA와 CPU cache 식별 레지스터

기존 AArch64 `CLIDR_EL1`과 `CCSIDR_EL1`은 0이었다. Linux가 cache level of
coherency를 0으로 읽으면 DT에서 non-coherent인 NPU도 coherent로 취급할 수
있다. NPU profile은 실제 구성의 CPU cache geometry를 아래 opt-in ISA
parameter로 전달한다.

- `cache_id_registers_enabled=True`
- `clidr`: L1/L2/L3 cache type 및 LoC/LoU field
- `ccsidr`: CSSELR `[3:0]` 순서의 14개 값. L1D=0, L1I=1, L2 unified=2,
  L3 unified=4이며 없는 cache는 0이다.

이 parameter는 guest의 식별 레지스터만 바꾸며 cache latency, bandwidth,
coherence protocol은 바꾸지 않는다. 기본값은 기존 동작을 보존한다.
32-bit CCSIDR 형식만 지원하므로 FEAT_CCIDX를 광고한 구성은 거절한다.
AArch32와 AArch64 read 경로 모두 같은 geometry를 제공하고 CSSELR 선택을
반영한다. AArch64 native 명령 실행 검사는 다음과 같다.

```sh
build/ARM/gem5.opt --outdir=/tmp/cache-id \
    tests/zena_cmn/directed/cache_id_v01.py
build/ARM/gem5.opt --outdir=/tmp/cache-id-default \
    tests/zena_cmn/directed/cache_id_v01.py --default
```

Fixture는 동반 `.S`를 `aarch64-linux-gnu-as`/`objcopy`로 빌드하여 실제
`MRS CLIDR_EL1`, `MSR CSSELR_EL1`, `ISB`, `MRS CCSIDR_EL1`을 실행한다.
16개 selector와 완료 marker를 guest RAM에서 읽는다. AArch32 명령 실행은
별도 검증 대상이다. Linux driver의 cache ownership/DMA 동기화와 데이터
가시성은 별도의 Linux NPU 통합 테스트로 확인해야 한다.

## 실행 결과 (2026-10-18)

첫 변경 전 native binary에서는 반복 unmapped write 12개 중 2개만 생성되고
1개만 응답하여 assertion으로 종료했다. Write-buffer가 4 beats이고 64 B
packet 하나가 전체 credit을 차지하는 구성으로 누락된 반환을 재현했다.
`build/gem5-zena/npu-smmu-before.log`에 기록했다.

18:06:04 빌드 binary에서는 다음 검사가 정상 종료했다.

- `build/gem5-zena/npu-smmu-v2/smmu-result.json`: **19개 PASS**.
- `build/gem5-zena/npu-cache-id-v2/cache-id-result.json`: 설정한 CLIDR 및
  CSSELR 16개 선택값을 실제 AArch64 명령으로 확인, **PASS**.
- `build/gem5-zena/npu-cache-id-default-v2/cache-id-result.json`: 기존
  AArch64 기본값(CLIDR=0, 모든 CCSIDR=0) 유지, **PASS**.

해당 binary SHA256:
`4c9341f93ca352c74a1a8403c31e9b1f84d518e7c6270e38175a69b7dbaeeb3b`.
이 검사는 SimpleMemory 경로이며 Ruby/CHI 및 Linux NPU 통합 결과를 대신하지
않는다. 후속 native binary가 변경되면 같은 세 검사를 다시 실행해야 한다.

CHI 변경을 포함한 `npu-gem5-build-chi4.log`의 후속 빌드에서도 같은 검사를
새 output directory에서 재실행했다. 세 프로세스 모두 exit code 0이다.

| 최종 재검증 결과 파일 | 결과 |
|---|---|
| `build/gem5-zena/npu-smmu-final/smmu-result.json` | 19개 PASS |
| `build/gem5-zena/npu-cache-id-final/cache-id-result.json` | 설정한 CLIDR 및 CCSIDR selector 16개 PASS |
| `build/gem5-zena/npu-cache-id-default-final/cache-id-result.json` | 기본 CLIDR=0 및 CCSIDR=0 유지 PASS |

재검증 binary SHA256:
`c9889dc5ebb6e130c83ef1232f629770468b3d7806a01f77b96fa9113b4c2469`.
Ruby/CHI 변경이 포함된 binary로 실행했지만 fixture 자체는 계속 SimpleMemory를
사용한다. CHI DMA 가시성과 Linux 부팅 검증은 해당 통합 결과를 별도로 참조한다.

최종 relink 뒤 production binary에서도 아래 세 검사를 다시 실행했다.
모두 exit code 0이며, 이 문서의 최종 binary provenance는 아래 SHA256이다.

| Production 결과 파일 | 결과 |
|---|---|
| `build/gem5-zena/npu-smmu-production/smmu-result.json` | 19개 PASS |
| `build/gem5-zena/npu-cache-id-production/cache-id-result.json` | 설정 CLIDR 및 CCSIDR selector 16개 PASS |
| `build/gem5-zena/npu-cache-id-default-production/cache-id-result.json` | 기본 cache ID 레지스터 유지 PASS |

Production binary SHA256:
`146260d0620253bc3062db7871aece670b9910a86b5236a1ee42d8906e1a0dfb`.

## Linux production ROI의 stage-1 사용 확인

`build/gem5-zena/npu-linux-production/smmu-roi-evidence.json`은 실제 Linux
재사용 BO 3회 실행의 첫 통계 block을 추출한 근거다. Guest의 Translated
domain 설정뿐 아니라 native SMMU가 stage-1 page table을 읽은 것을 확인한다.

| ROI 관측값 | 값 |
|---|---:|
| ROI 시간 | 859.893 µs |
| 완료 job / fault | 3 / 0 |
| DMA packet / translation samples | 39 / 39 |
| STE / CD fetch | 1 / 1 |
| Page-table walk samples | 9 |
| S1 walk-cache lookup L0 / L1 / L2 / L3 | 1 / 1 / 9 / 0 |
| S2 lookup, 모든 level | 0 |

CD fetch와 S1 lookup은 pure bypass에서 발생하지 않는다. 따라서 이 실행은
Linux가 구성한 native stage-1 translation을 실제 사용했다. 이 수치만으로
모든 mapping의 nonidentity 여부나 fault isolation까지 검증했다고 해석하지
않는다. 해당 항목은 앞의 directed 검사와 분리한다. 종료 시 추가되는 통계
block은 같은 reset 이후의 누적값이므로 첫 ROI block과 합산하지 않는다.
