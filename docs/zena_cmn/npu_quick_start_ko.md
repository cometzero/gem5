# Zena CMN NPU v0.1 실행

NPU는 opt-in이다. 기본 CPU-only profile에는 NPU/SMMU/추가 DRAM arbiter가 없다.
FUNC_OPS 제어 엔진이 private 2 MiB SRAM과 INT8 MAC을 제어한다.
실제 RV32 firmware 실행이나 실물 성능 보정은 지원 범위에 포함하지 않는다.

## 짧은 DRAM·fabric·NPU 포화 진단

Linux 부팅 없이 native 모델의 병목을 분리하려면 아래 명령을 사용한다.
출력은 새 디렉터리를 지정한다. 기존 기본 profile은 유지하며
`a720ae_npu_saturation.yaml`만 명시적으로 확장한 진단 구성이다.

```sh
python3 gem5/scripts/zena_cmn/run_saturation.py \
  --suite backend --output build/gem5-zena/saturation-backend-new
python3 gem5/scripts/zena_cmn/run_saturation.py \
  --suite npu --mib 4 --output build/gem5-zena/saturation-npu-new
```

최종 프로파일은 DMA512 B/cycle·issue8·credit1024, SRAM16 banks×32 B,
SMMU device slot1024, fabric512 B를 사용한다. NPU/SMMU1 GHz와
fabric/controller2 GHz 및 DRAM timing은 유지한다. 실제4 KiB stage-1 변환을
거치는16 MiB 검증에서 Read54.08/Write55.69/Copy56.37 GB/s를 기록했다.
Copy는 Read+Write 합계다. 단일 native 패킷 경로 제한으로 DRAM 참조 약250 GB/s에
도달한 것은 아니다. 실행 명령·단계별 수치·기능 검증·Linux와의 차이는
[포화 실험 보고서](../../../doc/gem5/npu_memory_saturation_report_ko.md)를 참조한다.

## 빌드

Workspace `/srv/oss_contrib`에서 실행한다. 기존 ARM multi-protocol 구성을 사용한다.

```sh
(cd gem5 && scons build/ARM/gem5.opt -j3)
JOBS=2 scripts/build_gem5_zena_linux.sh --profile npu
make -C npu_apps BUILD_DIR=/srv/oss_contrib/build/gem5-zena/npu-apps
```

커널 source fragment는 `gem5/configs/example/arm/zena_css_npu.config`, 산출물과
최종 config/build manifest는 `build/gem5-zena/linux-npu/`에 있다.
기존 firmware와 Buildroot initrd를 사용한다. 신규 환경에서는 기존 Zena CMN
[quick start](quick_start.md)에 따라 두 입력을 먼저 생성해야 한다.

## Linux 기능 검사

출력 경로는 존재하지 않는 새 디렉터리를 지정한다.

```sh
python3 gem5/scripts/zena_cmn/run_npu.py \
  --gem5 gem5/build/ARM/gem5.opt \
  --kernel build/gem5-zena/linux-npu/vmlinux \
  --bootloader build/gem5-zena/firmware/zena_css4.elf \
  --initrd build/buildroot/arm64/images/rootfs.cpio \
  --cpus 4 --channels 16 --timeout 1200 \
  --output build/gem5-zena/npu-linux-run
python3 gem5/scripts/zena_cmn/verify_config.py \
  build/gem5-zena/npu-linux-run/m5out
```

Runner는 NC PRIME 미지원 검사 후 Atomic→O3 전환하여 GEMM을 세 번 실행한다.
매번 CPU가 입력과 출력 버퍼를 다시 써서 DMA ownership 전환을 확인한다.
출력 행렬, job당 tensor DMA 384 B, MAC 512회, IRQ/fence 완료를 확인한다.
NC 경로에서는 PRIME import/export를 `EOPNOTSUPP`로 거절한다.
기존 coherent QEMU 경로의 PRIME 지원은 유지한다.

`result.json`은 실행 판정, `roi_summary.json`은 정확한 guest marker 구간의
회계, `guest_console.log`는 guest 검사 결과다. `m5out/config-verification.json`
검사는 구성 증거이며 실행 결과를 대신하지 않는다.
초기 기능 ROI에는 open/ioctl/map/reset/cache-maintenance 비용이 포함된다.
이 작은 GEMM의 bytes/time을 지속 가능한 DRAM 대역폭으로 해석하면 안 된다.

## 연결과 경계

- NPU MMIO `0x0b000000/64 KiB`, completion/fault INTID 112/113.
- SMMUv3 MMIO `0x0b100000/128 KiB`, EVTQ INTID 114, SID `0x100`.
- NPU payload: SMMU → NoncoherentXBar → 기존 LPDDR5X MemCtrl.
- SMMU STE/PTE/CMDQ/EVTQ: coherent CHI RNI → CPU Home → 같은 MemCtrl.
- NPU의 DT에는 `dma-coherent`가 없고 SMMU maintenance node에는 있다.
- 기본 1 GHz, 16 B/cycle link, 16 packet credit, SRAM 16 bank×4 B는 모델 가정이다.

SRAM은 CPU 전역 RAM으로 노출되지 않는다. RESET/ABORT는 이미 수락된 DMA를
배출한 뒤 완료한다. RESET은 SRAM zeroing 비용도 모델링한다.

## CPU/NPU 벤치마크

```sh
python3 gem5/scripts/zena_cmn/run_npu.py \
  --mode npu-bench --npu-workload concurrent --npu-mib 1 --passes 1 \
  --npu-cpu-delay-us 200 \
  --gem5 gem5/build/ARM/gem5.opt \
  --kernel build/gem5-zena/linux-npu/vmlinux \
  --bootloader build/gem5-zena/firmware/zena_css4.elf \
  --initrd build/buildroot/arm64/images/rootfs.cpio \
  --cpus 4 --channels 16 --timeout 1200 \
  --output build/gem5-zena/npu-concurrent-pilot
```

`--npu-workload cpu|npu|concurrent`로 같은 준비/측정 절차를 비교한다.
`--npu-async`는 별도 async profile과 package를 활성화한다. 기능이 없는 backend에
async 명령을 보내면 앱이 capability 단계에서 거절한다.

1 MiB는 초기 정답·회계 검사이며 검증한 호스트에서 약4–5.4분이 걸렸다.
짧은 NPU 반복 길이 비교에는 `--npu-workload npu --npu-mib 4 --passes 1`과
`--passes 2`부터 사용한다. 두 조건은 약7.6분에 정상 종료했으나 아직 처리율이
수렴하지 않았다. 추가4/8회는 각각 약7.2/7.5분에 완료됐고 payload 처리율 변화는
2.45%였지만 전체 처리율 변화는15.50%로 아직 미수렴이다. 짧은 payload 비교에는
`--npu-workload npu --npu-mib 4 --passes 8`을 사용할 수 있다. 4 MiB는 NPU SRAM보다 크며 NC payload가 CPU cache를 우회한다.
이 조건이 CPU DRAM working set 조건까지 만족한다는 뜻은 아니다.
16 MiB concurrent의1/2회 반복은 최초 20분 제한에서 timeout됐으며,
60분으로 늘린 재검증에서는 각각 31.7/38.7분에 정상 종료했다.
따라서 이를 20분 이내 실행의 기본값으로 권장하지 않는다. Host 시간은 장비와
동시 실행 부하에 따라 달라진다. CPU는 scalar read loop이므로 CPU 최대 load
issue 처리량으로 해석하지 않는다.
`roi_summary.json`의 participant 처리율과 공통 ROI의 DRAM burst byte 회계를
구분해서 읽는다. 동시 실행의 `overlap_ns`는 CPU worker와 NPU submit~fence 구간의
중첩이며, 각 master의 DRAM burst 중첩을 직접 증명하지 않는다.

CPU가 먼저 끝나면 `--npu-cpu-delay-us 200` 같은 시작 지연을 CPU-only와 concurrent
대조에 동일하게 적용할 수 있다. 이 값은 workload phase 제어이며 하드웨어 설정이
아니다. 실제 sleep 지연은 scheduler에 따라 달라진다. `payload_phase_overlap`은
marker 전후 clock bracket과1 µs guard 가정으로 개별 CPU worker 구간의 합집합과
NPU payload 수락~응답 구간을 비교한 **모델 시간 추정**이다. `ESTIMATED_NO_PHASE_OVERLAP`
이면 DRAM 경합 결과로 사용하지 않는다. `ESTIMATED_PHASE_OVERLAP`도 cycle별 burst의
동시 실행이나 지속 가능한 최대 대역폭을 보장하지 않는다. 기본 start barrier만으로
동시성이 보장되지 않으며 submit 구간도 겹치지 않으면 runner가 측정을 FAIL 처리한다.

## 장시간 반복 제출 검증

기존 바이너리를 보존하려면 별도 앱 출력 디렉터리를 사용한다.

```sh
make -C npu_apps BUILD_DIR=/srv/oss_contrib/build/gem5-zena/npu-apps-jobs
python3 gem5/scripts/zena_cmn/run_npu.py \
  --mode npu-bench --npu-workload concurrent --npu-mib 16 --passes 2 \
  --npu-jobs 4 --npu-cpu-delay-us 4000 \
  --npu-app build/gem5-zena/npu-apps-jobs/npu_v01_bench \
  --gem5 gem5/build/ARM/gem5.opt \
  --kernel build/gem5-zena/linux-npu/vmlinux \
  --bootloader build/gem5-zena/firmware/zena_css4.elf \
  --initrd build/buildroot/arm64/images/rootfs.cpio \
  --cpus 4 --channels 16 --timeout 5400 \
  --output build/gem5-zena/npu-long-concurrent-j4
python3 gem5/scripts/zena_cmn/verify_config.py \
  build/gem5-zena/npu-long-concurrent-j4/m5out
```

`--npu-jobs`의 기본값은1, 범위는1..32다. 각 job은64 MiB tensor DMA 한도를
유지하며16 MiB·passes2·jobs4의 누적 DMA는256 MiB다. CPU는 passes×jobs를
연속 실행한다. NPU-only에는 `--npu-cpu-delay-us`를 생략하고, CPU-only와
concurrent에는 같은 지연을 적용한다. 지연4000 µs는 이번16 MiB 대조 조건이다.

각 job의 상태·DMA byte·fence 완료를 확인하고 전체 데이터는 최종 출력만
비교한다. 중간 출력 내용은 검증하지 않았다고 JSON에 표시한다. Multi-job의
첫/마지막 payload timestamp에는 job 사이 공백이 있으므로 연속 payload의
처리율이나 중첩으로 해석하지 않는다. 결과와 수렴 판정은
[장시간 검증 보고서](../../../doc/gem5/npu_v01_extended_validation_ko.md)를 따른다.

측정한 host 시간은 jobs2→4에서 CPU-only 29.9→48.0분, NPU-only 28.2→32.7분,
concurrent 43.1→59.9분이었다. 세 조건의 처리율 변화는 모두 5% 이내였다.
90분 제한은 이 환경의 여유값이며 장비별 실행시간을 보장하지 않는다.
최종 4-job은 정상 종료 후 12자리 native 통계의 공백 처리 오류로 후처리가
실패했으므로 수정된 `summarize_roi.py`를 함께 사용해야 한다. 원본 재분석은 PASS다.

## Cache ID 및 maintenance

NPU profile은 실제 구성한 L1/L2/L3의 CLIDR/CCSIDR을 guest에 알린다.
기본 gem5의 CLIDR=0은 Linux가 장치를 coherent로 강제 전환하게 하므로 NC 검사에
사용할 수 없다. 이것은 CPU 모델명을 A720AE로 바꾸거나 성능 큐를 늘리는 설정이
아니다. 기본 CPU-only profile의 cache ID 동작은 유지한다.

구현·검증의 실제 통과 범위는 [통합 보고서](../../../doc/gem5/npu_v01_implementation_validation_ko.md),
[NPU 모델](npu_model.md), [SMMU 검사](smmu_v01_regressions.md)를 참고한다.
