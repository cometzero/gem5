# Linux cache sysfs 가시성 제한

현재 Ruby 구성과 DT에는 코어별 L1I/L1D/private L2, 클러스터별 shared L3가
존재하지만, Linux cache sysfs 검사는 **UNSUPPORTED**다. DT의 구조 검증과
Linux의 실행 중 cacheinfo 등록 성공은 서로 다른 증거다.

## 원인

현재 [`src/arch/arm/isa.cc`](../../src/arch/arm/isa.cc)의
`MISCREG_CLIDR` 읽기는 `0x00200000`을 반환한다. LoUIS 필드만 1이며
각 레벨의 cache-type 필드는 모두 0이다. `MISCREG_CCSIDR`도 구현되지
않았다. `ArmISA.py`에는 이 값을 올바른 topology 값으로 바꾸는 native
configuration parameter가 없다. MIDR 변경은 이 동작을 해결하지 못한다.

사용 중인 Linux 소스의 다음 경로가 충돌한다.

1. `drivers/base/cacheinfo.c:init_of_cache_level()`은 DT의 L1I/L1D와
   L2/L3를 세어 **3 levels, 4 leaves**를 할당한다.
2. `arch/arm64/kernel/cacheinfo.c:populate_cache_leaves()`는 CLIDR의
   cache type을 읽는다. split L1 type이 없으므로 레벨당 한 leaf만 채운다.
3. 결과는 `(level,type) = (1,0), (2,0), (3,0), (0,0)`이다. 마지막
   leaf는 초기값으로 남는다.
4. `drivers/base/cacheinfo.c:cache_setup_of_node()`는 level 1 이외의
   leaf에서 다음 DT cache node로 이동한다. L2/L3의 `cache-unified`는
   type 0을 unified로 보완하지만, 마지막 level-0 leaf에서는 L3 뒤의
   존재하지 않는 노드를 찾게 되어 `-ENOENT`로 실패한다.

CPU DT node에 `i-cache-size`/`d-cache-size`를 추가해도 cache type을
채우는 arm64 경로가 달라지지 않으므로 해결되지 않는다. L1을 unified로
허위 표시하면 leaf 수만 맞출 수 있지만 실제 split L1 모델과 모순된다.
이 방법과 MIDR 위장은 사용하지 않는다.

## 재현 가능한 진단

workspace 루트에서 다음을 실행한다.

```sh
python3 gem5/tests/zena_cmn/test_cache_visibility.py
python3 gem5/scripts/zena_cmn/check_cache_visibility.py \
  build/gem5-zena/cmn-fs-first
```

probe는 실제 `config.json`, `resolved-profile.json`, `system.dtb`를
읽고 기존 DT 검증기를 호출한다. 현재 ISA 소스의 CLIDR 상수와 Linux의
leaf 생성 규칙을 대조해 `cache-visibility.json`에 CPU별 DT chain,
할당/생성 leaf, source/DTB SHA-256을 남긴다. 예상 제한을 재현하면
프로세스는 0으로 종료하지만 결과 상태는 **UNSUPPORTED**다. 종료 코드
0을 Linux cache visibility PASS로 해석하면 안 된다.

이 검사는 guest에서 직접 CLIDR을 읽거나 sysfs를 재측정하지 않는다.
ISA 소스와 과거 실행 바이너리가 같은지는 별도 manifest로 확인해야 한다.
CLIDR 소스가 상수가 아닌 구현으로 바뀌면 probe가 중단되어 재검토를 요구한다.

## 후속 수정의 범위

정확한 해결은 별도 구현·검증 작업이다.

- ISA 쪽에서는 실제 cache topology/geometry와 일치하는 CLIDR·CCSIDR·
  CSSELR 동작과 관련 cache-maintenance 의미를 함께 검증해야 한다.
  cache type만 임의 설정하고 CCSIDR을 0으로 두면 일관된 모델이 아니다.
- Linux 쪽에서는 CLIDR에 정보가 없는 경우 firmware의 split-cache
  leaf type을 정확히 사용하는 fallback을 검토할 수 있다. 이는 별도 kernel
  변경이며 CPU hotplug, shared cache map, 다른 DT/ACPI 시스템 회귀가 필요하다.
- CLIDR LoC는 `arch/arm64/mm/dma-mapping.c:arch_setup_dma_ops()`의
  non-coherent DMA 처리에도 영향을 준다. sysfs 출력만을 위해 변경하면
  미래 NC NPU의 가시성과 cache maintenance 계약을 바꿀 수 있다.

이번 작업은 진단만 추가했다. gem5 ISA/core, MIDR, Linux 소스 및 모델의
cache 동작은 변경하지 않았다. 따라서 DT와 Ruby topology PASS는 유지하되
Linux L2/L3 `shared_cpu_list`의 실행 검증은 미지원으로 구분한다.
