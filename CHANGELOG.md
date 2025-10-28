## [1.0.1](https://github.com/StanislavBolshakov/ha-eirc/compare/v1.0.0...v1.0.1) (2025-10-27)

### Bug Fixes

* **sensor:** добавлен translation_key ([416f1df](https://github.com/StanislavBolshakov/ha-eirc/commit/416f1df69e5d0104ea2129a5e0045e987b6ea1e1))

# [1.0.0](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.11.3...v1.0.0) (2025-10-26)


### Features

* **API:** - централизация логики запросов и удаление дублирования кода ([21aca4f](https://github.com/StanislavBolshakov/ha-eirc/commit/21aca4f6032269f3b7247e2032b532bae9216e1b))
* единый скоординированный опрос для всех entity, управляемых интеграцией ([a40be32](https://github.com/StanislavBolshakov/ha-eirc/commit/a40be3288d2e053850ac8de35f739ee34c5fc9ed))


### BREAKING CHANGES

* **API:** конструктор EIRCApiClient был изменен и больше не принимает токены и cookie

## [0.11.3](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.11.2...v0.11.3) (2025-09-20)


### Bug Fixes

* **EIRC:** сервис отправления показаний теперь корректно обновляет данные сенсора после отправки ([db77da2](https://github.com/StanislavBolshakov/ha-eirc/commit/db77da226ca9d95b65a2069f83cf96ddde264496))

## [0.11.2](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.11.1...v0.11.2) (2025-09-16)


### Bug Fixes

* **EIRC:** добавлена логика API retry для 400 ошибок ([95ae34a](https://github.com/StanislavBolshakov/ha-eirc/commit/95ae34ae504f26f232de55fa6b82bd7d464b480c))

## [0.11.1](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.11.0...v0.11.1) (2025-09-16)


### Bug Fixes

* **EIRC:** корректная инициализация клиента EIRC в сервисе ([4384f32](https://github.com/StanislavBolshakov/ha-eirc/commit/4384f32a95366df103b4ff4181b6bccd8207109a))

# [0.11.0](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.10.3...v0.11.0) (2025-09-16)


### Features

* **EIRC:** добавлена поддержка 2FA в ЛК ЕИРЦ ([04c2828](https://github.com/StanislavBolshakov/ha-eirc/commit/04c282865823122d2ce32eadeb471520d8162bec))

## [0.10.3](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.10.2...v0.10.3) (2025-02-19)


### Bug Fixes

* **EIRC:** reverted scan interval to 15 minutes ([cc6f5ca](https://github.com/StanislavBolshakov/ha-eirc/commit/cc6f5ca7c3577ae2a0f74bb36c66ab7a5c58d8b3))

## [0.10.2](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.10.1...v0.10.2) (2025-02-19)


### Bug Fixes

* **EIRC:** EIRCSensor correctly fetch Update Coordinator data ([217100b](https://github.com/StanislavBolshakov/ha-eirc/commit/217100b439ee914f018921089e028614716d6cf7))

## [0.10.1](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.10.0...v0.10.1) (2025-02-19)


### Bug Fixes

* **EIRC:** EircMeterSensor correctly fetch update from Update Coordinator ([30bd07e](https://github.com/StanislavBolshakov/ha-eirc/commit/30bd07efa420b518daffa5ffd5c603a31ad9d79a))

# [0.10.0](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.9.3...v0.10.0) (2025-02-16)


### Features

* **EIRC:** impl DataUpdateCoordinator pattern to reduce API spam and 429 rate ([56836f7](https://github.com/StanislavBolshakov/ha-eirc/commit/56836f73f8b6734b3f8e987137ce33e574627837))

## [0.9.3](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.9.2...v0.9.3) (2025-02-15)


### Bug Fixes

* **EIRC:** 429 error handling, sensor update time and API backoff timer adjustments ([23480f5](https://github.com/StanislavBolshakov/ha-eirc/commit/23480f5a257334dadec648586142785a3f879733))

## [0.9.2](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.9.1...v0.9.2) (2025-02-13)


### Bug Fixes

* **EIRC API:** 500 errors are properly handled ([72ce055](https://github.com/StanislavBolshakov/ha-eirc/commit/72ce055c709d92ab592ea8e1932d88431c3d2f28))

## [0.9.1](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.9.0...v0.9.1) (2025-02-12)


### Bug Fixes

* **eirc:** config flow deprecation warning fixed, Russian translation updated ([db8c32e](https://github.com/StanislavBolshakov/ha-eirc/commit/db8c32e40814d6bfc6468f6bba7a2f38530007e3))
* **eirc:** config flow deprecation warning fixed, Russian translation updated ([35f742b](https://github.com/StanislavBolshakov/ha-eirc/commit/35f742b786552b5dd814dbd2fc5f92cf762b8287))

# [0.9.0](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.8.0...v0.9.0) (2025-02-12)


### Features

* **EIRC:** fixed Home Assistant deprecation warnings ([cd57aa3](https://github.com/StanislavBolshakov/ha-eirc/commit/cd57aa3edbc81fdf15532e306618b398537c479e))

# [0.8.0](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.7.0...v0.8.0) (2025-02-11)


### Features

* **eirc:** Update Meter Readings service added ([e8237c7](https://github.com/StanislavBolshakov/ha-eirc/commit/e8237c795f4dcbee3dc3ac3f924d4a7e09f2d878))

# [0.7.0](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.6.1...v0.7.0) (2025-02-10)


### Features

* **eirc:** updated russian translation ([5911785](https://github.com/StanislavBolshakov/ha-eirc/commit/591178513cdbe093a8a9ae39bde454112b219cdb))

## [0.6.1](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.6.0...v0.6.1) (2025-02-09)


### Bug Fixes

* adj sensor naming ([6229b5f](https://github.com/StanislavBolshakov/ha-eirc/commit/6229b5f25b11d916c1acae2c9ad24fd0e41ad357))

# [0.6.0](https://github.com/StanislavBolshakov/ha-eirc/compare/v0.5.3...v0.6.0) (2025-02-09)


### Bug Fixes

* bugfix bump.sh ([5ed1c2d](https://github.com/StanislavBolshakov/ha-eirc/commit/5ed1c2d4e012869aa3ef5fc30ed7d704dcedd96c))


### Features

* **eirc:** component is completely rewritten for the new /v8 API endpoint. Added meters other than electricity. Added accounts. Added Config Flow integration setup. ([2f62151](https://github.com/StanislavBolshakov/ha-eirc/commit/2f6215165a3d02fbf0a8d8813a666ba6bf14dcd7))

## [0.5.3](https://github.com/StanislavBolshakov/ha-pes/compare/v0.5.2...v0.5.3) (2023-08-23)


### Bug Fixes

* **ha-pes:** fix async platform setup errors ([dde389e](https://github.com/StanislavBolshakov/ha-pes/commit/dde389ef5a2b1921c4c17b9e54d04d4b40427187))

## [0.5.2](https://github.com/StanislavBolshakov/ha-pes/compare/v0.5.1...v0.5.2) (2023-08-23)


### Bug Fixes

* **ha-pes:** removed service due to asyncio failure ([820aebf](https://github.com/StanislavBolshakov/ha-pes/commit/820aebf95aee8be15b6bb787065c1a0856756934))
* **ha-pes:** removed service due to asyncio failure ([849b8ed](https://github.com/StanislavBolshakov/ha-pes/commit/849b8eded155ab3414d20bb08accba3dd94c1140))

## [0.5.1](https://github.com/StanislavBolshakov/ha-pes/compare/v0.5.0...v0.5.1) (2023-08-23)


### Bug Fixes

* **ha-pes:** fix import RestClient ([17b95dd](https://github.com/StanislavBolshakov/ha-pes/commit/17b95dd2c2119411e8718abb3516b77372d29c95))

# [0.5.0](https://github.com/StanislavBolshakov/ha-pes/compare/v0.4.3...v0.5.0) (2023-06-20)


### Features

* **pes:** workaround short token expiration time ([7324025](https://github.com/StanislavBolshakov/ha-pes/commit/7324025b2acc211b2464baaf73318ac314815655))

## [0.4.3](https://github.com/StanislavBolshakov/ha-pes/compare/v0.4.2...v0.4.3) (2023-04-12)


### Bug Fixes

* **pes:** add logic to handle faulty updates ([065d256](https://github.com/StanislavBolshakov/ha-pes/commit/065d256791181a1ec0082b58ae237efa32b95196))

## [0.4.2](https://github.com/StanislavBolshakov/ha-pes/compare/v0.4.1...v0.4.2) (2023-04-12)


### Bug Fixes

* **pes:** fix ha retry decorator deprecated ([75f4c0f](https://github.com/StanislavBolshakov/ha-pes/commit/75f4c0fde12266b53760b7d1aa3be6d6386918a0))

## [0.4.1](https://github.com/StanislavBolshakov/ha-pes/compare/v0.4.0...v0.4.1) (2023-04-12)


### Bug Fixes

* **pes:** enable platform setup retry flow in case PES api wasn't aviable after reboot ([d98a359](https://github.com/StanislavBolshakov/ha-pes/commit/d98a35955401bc0b81dd0195c258756bdc0e8778))

# [0.4.0](https://github.com/StanislavBolshakov/ha-pes/compare/v0.3.2...v0.4.0) (2023-04-12)


### Features

* **pes:** adjust directory structure to be HACS compliant ([795abb9](https://github.com/StanislavBolshakov/ha-pes/commit/795abb914cd2b3b755ef0d1a7ddccb2777fae068))

## [0.3.2](https://github.com/StanislavBolshakov/ha-pes/compare/v0.3.1...v0.3.2) (2023-03-25)


### Bug Fixes

* **pes:** remove mobile client attribute from POST header ([65ee518](https://github.com/StanislavBolshakov/ha-pes/commit/65ee518b5c64730bda50cda657f0bc3de89052e8))

## [0.3.1](https://github.com/StanislavBolshakov/ha-pes/compare/v0.3.0...v0.3.1) (2023-03-25)


### Bug Fixes

* **pes:** remove mobile client attribute from headers ([ba8bc79](https://github.com/StanislavBolshakov/ha-pes/commit/ba8bc793fd2b92ca2de4f8877e32a6df5fbce8a6))

# [0.3.0](https://github.com/StanislavBolshakov/ha-pes/compare/v0.2.3...v0.3.0) (2023-02-27)


### Features

* **pes:** trigger release ([5a20a84](https://github.com/StanislavBolshakov/ha-pes/commit/5a20a84b74123b79890236f36b68fd6cc511a694))
* **pes:** adjust API endpoint after update, adjust README, handling auth error due to outdated token ([358794e](https://github.com/StanislavBolshakov/ha-pes/commit/358794e00c8aef42bfb6f84dd2c06ca5e20306e1))

## [0.2.3](https://github.com/StanislavBolshakov/ha-pes/compare/v0.2.2...v0.2.3) (2023-01-09)


### Bug Fixes

* **pes:** accidental removal of sensor update callback ([aaea115](https://github.com/StanislavBolshakov/ha-pes/commit/aaea115c2bbef2c96cc3255b239e066d756d7918))

## [0.2.2](https://github.com/StanislavBolshakov/ha-pes/compare/v0.2.1...v0.2.2) (2023-01-09)


### Bug Fixes

* **pes:** ensure INT as incremental update value ([b068b25](https://github.com/StanislavBolshakov/ha-pes/commit/b068b25ee58a358332f1f1589373992ca9420794))

## [0.2.1](https://github.com/StanislavBolshakov/ha-pes/compare/v0.2.0...v0.2.1) (2023-01-09)


### Bug Fixes

* **pes:** typo in service name ([6b35a95](https://github.com/StanislavBolshakov/ha-pes/commit/6b35a95423c2029cc40d256057d89784dd67201b))

# [0.2.0](https://github.com/StanislavBolshakov/ha-pes/compare/v0.1.2...v0.2.0) (2023-01-09)


### Features

* **pes:** New Incremental Update Service ([c23f310](https://github.com/StanislavBolshakov/ha-pes/commit/c23f310d37e625ab76dd59eb97d7d9e645e39356))

## [0.1.2](https://github.com/StanislavBolshakov/ha-pes/compare/v0.1.1...v0.1.2) (2023-01-05)


### Bug Fixes

* reorder steps ([fe1c2df](https://github.com/StanislavBolshakov/ha-pes/commit/fe1c2dfc9f7c37a6f2f3f1986ea618fcb37287c4))

## [0.1.1](https://github.com/StanislavBolshakov/ha-pes/compare/v0.1.0...v0.1.1) (2023-01-05)


### Bug Fixes

* add execute permission ([f187b3c](https://github.com/StanislavBolshakov/ha-pes/commit/f187b3c8737274206f33c799076e9af81e39e5dd))

# [0.1.0](https://github.com/StanislavBolshakov/ha-pes/compare/v0.0.1...v0.1.0) (2023-01-05)


### Bug Fixes

* change env variable ([4c8d258](https://github.com/StanislavBolshakov/ha-pes/commit/4c8d2582ac3ead067e44f77dd9918b7a62a8fa0e))
* dependency ([b2c46ff](https://github.com/StanislavBolshakov/ha-pes/commit/b2c46ff0247e50b79c7667465c5ad3b41581f74a))
* publishCmd ([bc31932](https://github.com/StanislavBolshakov/ha-pes/commit/bc31932c0ee587c277ac1ec053bb249d2caa5a92))
* semantic release ([26a85d6](https://github.com/StanislavBolshakov/ha-pes/commit/26a85d6a32970e52cf333b0574921f145d5581a9))
* semantic release workflow ([a1246be](https://github.com/StanislavBolshakov/ha-pes/commit/a1246be955bb4ccd181571dedd0e77dcecf44ee1))


### Features

* **pes:** Semantic Release ([6346f1b](https://github.com/StanislavBolshakov/ha-pes/commit/6346f1bebf37c1e57fc0071a3bf39fe6fc645484))
