# 짧은 Linux read ROI 수렴·channel phase 검증

`check_memory_convergence.py`는 같은 public candidate의4 CPU/4 worker,
16채널, NEON read16 MiB, timing warm-up1회 조건에서 ROI1/2/4회를 비교한다.
phase 대조는2회 baseline과 맞춘다. 모델 설정·runner를 변경하지 않는다.

```sh
python3 scripts/zena_cmn/check_memory_convergence.py \
  --base ../build/gem5-zena/improvements-20261018 \
  --output ../build/gem5-zena/improvements-20261018/memory-convergence.json
```

각 case의 `result.json`이 완료된 뒤 실행한다. 최신 `verify_config.py`가 생성한
`m5out/config-verification.json`이 필요하며 artifact hash binding과 DT PASS를
재확인한다. 누락·stale·실패 데이터는 예외로 거부하며 측정 PASS를 출력하지 않는다.

검사 범위:

- guest 정상종료/4 CPU online/checksum/유효 byte/working set/반복수/warm-up 확인.
- ROI marker와 정확히 일치하는 stats block만 사용하며16채널 Read/Written
  counter의 누락을 거부한다.
- gem5/kernel/bootloader/base initrd/guest executable/profile SHA는 파일과 대조하고
  모든 case가 같아야 한다. 실행별 guest archive는 명령인자 차이로 달라도 되지만
  각 파일 hash를 검사한다.
- configuration source hash, resolved profile, 실제 config 전체의 동등성을
  확인한다. 실행 디렉터리 문자열과 host console의 임시 port만 정규화한다.
- phase test의4개 `CMN_LAYOUT` 주소가 worker별4 MiB+64 B 간격인지 확인하고
  page offset 내 bits6..9만 추출한다. 물리 page 배치나 bank 배치는 추정하지 않는다.
  현재 Home도 `(PA >> 6) & 7`로 선택되므로 Home phase도 함께 변한다.
  성능 차이를 DRAM channel만의 효과로 분리하는 실험은 아니다.

**측정 PASS와 수렴 PASS는 다르다.** 마지막 p2→p4의 유효 GB/s 변화 및
DRAM read bytes/useful bytes 비율 변화가 각각5% 이내일 때만 bounded convergence
PASS이다. p1→p2 변화도 별도 기록한다. 이5%는 프로젝트의 제한된 비교 기준이며
최대 지속 대역폭이나 실제 SoC 성능을 보증하지 않는다. phase p2는동일p2와의변화만
출력하며 향상 여부를 성공 조건으로 삼지 않는다.

DRAM counter는 OS/background를 포함한 aggregate이다. 물리주소필터를 적용한
애플리케이션 전용 계측이 아니다. 호스트 시간은 동시작업에 영향을 받는다.
JSON에는 provenance·layout·검사 결과, 같은 이름의 CSV에는 비교표를 저장한다.

## 2026-10-18 실행 결과

위 경로의 네 조건 모두 정상 종료와 artifact 검사를 통과했다.

| ROI 반복 | phase 분산 | useful GB/s | DRAM Read GB/s | host 분 |
|---|---|---:|---:|---:|
| 1 | 없음 | 12.287 | 12.356 | 11.81 |
| 2 | 없음 | 12.371 | 12.441 | 15.00 |
| 4 | 없음 | 12.495 | 12.551 | 20.46 |
| 2 | 있음 | 12.253 | 12.332 | 14.74 |

2→4회의 useful bandwidth 변화+1.003%, DRAM/useful 비율 변화−0.1204%로
bounded convergence PASS다. 짧은 회귀에는 이 구성의2회를 권장한다.
Phase 분산은 baseline2회 대비−0.956%로 개선되지 않아 기본값으로 채택하지 않는다.
각 조건1회 실행이며 독립 반복의 신뢰구간이나20분 제한을 보장하지 않는다.
4코어 결과를16코어 포화 성능이나 실물 예측값으로 사용하지 않는다.
