# SK hynix LPDDR5X 공개 자료 후보 검토

상태: **별도 후보 구현 및 native traffic 검증 완료**. Micron 클래스와 별도 후보이다.
[H58G56CK8BX146 Rev.1.0](https://www.uttc.com.tw/wp-content/uploads/2026/02/H58G56CK8BX146_Rev1.0-3.pdf)
은 제조사 표기 문서의 공개 미러이며, 문서에는 `SK hynix Confidential` 표시가 있다.
Rev.1.0 날짜는 2024-03, 619쪽이다. SHA256:
`014fe94fe039e060c324d4e3af2f34a29c399bb25743654d05a40a2ab9eb96cc`.
아래 페이지는 PDF의 1-based 번호이며 주요 표를 렌더링하여 확인했다.

| 근거 | 후보 매핑 |
|---|---|
| p3 부품 정보 | 8533 speed bin, 4GiB package, 2채널/1CS → 채널당 2GiB, x16 |
| p22 표6 | BG4×bank4, row 2KiB, bank당 65,536 rows |
| p532 표396 | `tRCD=18ns`, `tRCD_WR=8ns`, `tRP=18ns`, `tRAS=42ns`, `tWR=34ns` |
| 동일 표 | `tRRD/tRRD_L=3.75ns`, `tXAW=15ns`, activation limit4 |
| 동일 표 | `tWTR=6.25ns`, `tWTR_L=12ns`, `tPPD=2CK` |
| p269 표225 | RL set0=23CK; DBI/data-copy/byte-mode 비활성 |
| p273 표229 | WL setA=12CK, setB=22CK; 명시적으로 하나 선택 필요 |
| p295 표240 | 16Gb/channel: `tRFC=280ns`, `tREFI=3.906us`, ≤85°C 1×refresh |
| p305 표242 | `tXS=287.5ns` 유도 가능 |

표396은 x16/BG, DVFSC·Enhanced DVFSC·Link ECC 비활성 조건이다.
따라서 기존 Micron 요약보다 array/read/write/refresh 근거가 풍부하다.
Automotive Zena 실장 부품이나 실측 결과는 아니다.

native 의미를 다음과 같이 반영했다.

- `dram_interface.cc`는 ACT 후 read/write 제한을 분리하므로 18/8ns 매핑이 가능하다.
  그러나 `tRP` 하나로 all-bank21ns와 per-bank18ns를 완전히 표현하지 못한다.
- `tCWL`은 기본적으로 `Self.tCL`이므로 WL을 명시해야 한다.
  `mem_interface.hh::readToWriteDelay()`는 `tBURST+tRTW`이다.
  기존 1CK turnaround는 RL=WL 가정이므로 복사하지 않았다.
- `tRTP`는 native에서 READ command부터 PRE 제한이다. 표의 burst-end 기준
  `tRBTP`를 직접 대입하지 않고 BL32 command 관계로 변환했다.
- CK 표 상한1066.5MHz와 기존1066.6667MHz는 반올림 차이가 있다.
  정확한 선택 주파수와 cycle→tick 올림 정책을 별도로 명시해야 한다.
- 기존 2GiB aperture는 물리 용량과 다르다. native rows는 aperture 기준이므로
  `device_size=2GiB`만 바꿔 full geometry를 검증했다고 할 수 없다.
- BL32 gap, WCK sync, power-down, ODT/turnaround, 전력 모델은 추가 매핑 대상이다.
  일부 표 값 적용만으로 전체 부품 보정 완료를 선언하지 않는다.

원본·텍스트·확인 이미지는 ignored build 경로
`build/gem5-cmn/lpddr5x-datasheet/hynix-candidate/`에 보관했다.
기존 synthetic/Micron 기본값과 측정 결과는 변경하지 않았다.

## 적용한 선택과 검증

`configs/zena_cmn/lpddr5x_hynix.py`의 `MODE`와
`PARAMETER_PROVENANCE`에 선택 조건 및 native 값의 출처를 기록했다.
938ps CK, RL23/WLA12, HF mode, RDQS on, ODT/NT-ODT/DFE/training off를
고정한다. array 시간은 최소 제약을 줄이지 않도록 CK 단위로 올림했다.

표340(p491)의 READ→PRE는 6CK+6CK이므로 `tRTP=12CK`이다.
WRITE→PRE의 추가1CK는 native readyTime에 없으므로 `tWR=38CK`에 포함했다.
표347/348(p496–497) 및 표462(p592)의 HF output-offset 최대1600ps로
`tRTW=(23-12+ceil(1600/938))CK=13CK`를 얻는다.
전기적 variation 전체 보증이나 ODT 활성 모드로 일반화하지 않는다.

실행은 `tests/zena_cmn/directed/lpddr5x_backend_traffic.py --hynix`와
실제 `build_memory_backend()`를 사용했다. saved native binary
`build/gem5-zena/calibration-binary/gem5-before-exclusive.opt`로
10µs warm-up, 20µs ROI, 2µs tail, 채널별 outstanding64를 적용했다.

| 채널/traffic | DRAM burst bytes | GB/s |
|---|---:|---:|
| 4 read | 1,253,120 | 62.6560 |
| 8 read | 2,506,240 | 125.3120 |
| 16 read | 5,012,480 | 250.6240 |
| 16 write | 4,968,448 | 248.4224 |
| 16 50/50 | read 2,008,064 + write 1,916,928 | 196.2496 |

938ps CK의 16채널 raw 한도는 272.9211GB/s이다.
mixed 실제 생성량은 3,972,096B, 응답량은 3,916,800B이다.
새 요청 없는 tail에서 read56,320B/write44,032B가 추가 서비스되었다.
endpoint queue 잔여량은 native stats에 없으므로 추정하지 않았다.
write frontend 응답은 DRAM 완료가 아니다.

`check_backend_traffic.py`가 실제 config.json의 용량·RL/WL·tRTP/tWR/tRTW와
각 채널 burst accounting을 검사하여 5건 PASS했다.
증거는 `build/gem5-cmn/lpddr5x-datasheet/hynix-summary.json`이다.
이는 독립 generator 경로이며 Linux/CPU/실물 대역폭 검증이 아니다.

### tAAD 출처 반영 후 재검증

§7.3.1의 ACT-1→ACT-2 최대 간격 8CK를 적용하여 `tAAD`를 기존 상속값
7.500ns에서 **7.504ns**로 수정했다. 최초 5개 실험과 당시 실행 중이던
Linux artifact는 이전 값을 사용하므로 최신 클래스 검증과 구분한다.
이전 결과를 덮어쓰지 않았다.

`hynix16read-aad8ck`와 `hynix16mixed-aad8ck`를 새로 실행했고,
config.json의 16개 controller 모두 `tAAD=7504ps`임을 확인했다.
read 250.6240GB/s, mixed 196.2496GB/s로 위 20µs ROI 결과와 같았다.
결과는 `build/gem5-cmn/lpddr5x-datasheet/hynix-aad8ck-summary.json`이다.
이 짧은 traffic 대조에서 차이가 없다는 사실로 이전 Linux artifact를
최신 설정으로 실행했다고 재분류하지 않는다.

## Refresh 및 native queue 민감도

`lpddr5x_backend_traffic.py --hynix --diagnostic`에 독립 대조를 추가했다.
Hynix 클래스와 기본 profile은 변경하지 않는다. factory 생성 이후의 명시적
진단 override도 effective parameter/fingerprint로 다시 기록한다.

- `baseline`: tREFI3.906µs, read/write queue64/64.
- `mr4-half-interval`: tREFI만1.953µs로 변경. PDF p148 표70의 MR4=01011B
  공통 사양에 따른 half-interval 진단이다. 이 부품 p3의 최대85°C 범위 밖을
  보증하는 hot-part 설정이 아니며 온도 센서/MR4 read 동작도 구현하지 않는다.
- `queue16`: queue만16/16. FRFCFS, close-adaptive, write threshold85%/50%,
  min read/write switch16은 유지한다. 작은 queue와 기존 scheduling 정책의
  결합 효과이며 실제 Zena controller queue 크기를 주장하지 않는다.

16채널, outstanding64/channel, 2ns 요청 간격, 10µs warm-up/20µs ROI/2µs tail을
동일하게 적용했다. 여섯 native 실행과 config.json 검증이 PASS했다.

| 조건 | traffic | DRAM GB/s | read 평균 지연 ns | read queue 지연 ns | read/write burst bytes |
|---|---|---:|---:|---:|---:|
| baseline | read | 250.6240 | 238.56 | 211.36 | 5,012,480 / 0 |
| half interval | read | 227.1232 | 270.47 | 243.27 | 4,542,464 / 0 |
| queue16 | read | 122.4192 | 133.85 | 106.65 | 2,448,384 / 0 |
| baseline | mixed | 196.2496 | 557.57 | 530.36 | 2,008,064 / 1,916,928 |
| half interval | mixed | 176.7936 | 591.32 | 564.11 | 1,803,264 / 1,732,608 |
| queue16 | mixed | 131.2768 | 198.63 | 171.43 | 1,314,816 / 1,310,720 |

Refresh 빈도 증가에서 read는9.38%, mixed는9.92% 감소했다.
queue16에서 read는51.15%, mixed는33.11% 감소했다.
queue16의 지연 감소는 더 빠른 종단 처리를 뜻하지 않는다. controller 수락 전
backpressure가 증가한다. native read/write retry 합계는 각각 read 실험에서
38,240/0, mixed에서6,896/9,088이다. 이 지연은 `dram_interface.cc`의
`totMemAccLat/readBursts`, `totQLat/readBursts`이며 generator 첫 시도부터의
전체 지연이나 write latency가 아니다.

모든 조건의 명목 offered traffic은10,240,000B이다. 실제 생성량과 serviced
burst는 서로 다른 경계이며 ROI 이전 outstanding 때문에 같을 필요가 없다.
예를 들어 half-interval read는4,517,888B 생성,4,542,464B 서비스이다.
각 결과의 생성/응답량, 방향별 burst, tail 서비스량 및 retry는
`build/gem5-cmn/lpddr5x-datasheet/hynix-sensitivity-summary.json`에 보관했다.
잔여 queue endpoint는 미제공 상태를 유지한다. 이 결과는 native controller
민감도이며 CPU/Linux 또는 실물 열환경 성능 측정이 아니다.
