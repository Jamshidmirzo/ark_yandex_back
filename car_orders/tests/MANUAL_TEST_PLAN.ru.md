# `car_orders` — План ручного QA-тестирования

*Перевод с английского оригинала [MANUAL_TEST_PLAN.md](MANUAL_TEST_PLAN.md). При расхождениях англоязычная версия первична.*

Подробный обзор уровня senior-QA для всей функциональности `car_orders` (нативный
жизненный цикл `CarOrder` + trip-state наложения (overlay) `OrderMeta` + диспетчеризация
+ планирование + геозоны + живое отслеживание (live-tracking) + смены + права доступа).

Используйте его для приёмки релиза (release sign-off), исследовательских прогонов
и тех случаев, которые автоматизированный набор тестов **не способен** покрыть на
БД по умолчанию (конкурентность — см. §8).

## Как пользоваться
- Каждый кейс: **Pre** (подготовка) → **Steps** (шаги) → **Expect** (ожидаемый результат) → **Sev** (Blocker/High/Med/Low) → Pass/Fail.
- Роли: **Requester** (`car_order:create`), **Dispatcher** (`car_order:approve`/`reject`/`list`),
  **Driver** (`driver:accept_order`/`driver:trip_control`).
- Запуск бэкенда + базовые URL: см. заметку памяти dev-runtime. По умолчанию `REQUIRE_OVERLAY_AUTH=False`
  (открытый dev-режим). Для кейсов с аутентификацией запускайте с `REQUIRE_OVERLAY_AUTH=True`.
- Для большинства строк ниже существует «зелёный» автоматизированный тест — ID в `[квадратных скобках]` указывает на него;
  ручной фокус — на поведении UI/реального устройства/таймингов/интеграции, которое тест не может проверить.

---

## 1. Жизненный цикл заказа (нативный CarOrder)

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|1.1| Requester авторизован | Create → Submit → (Dispatcher) Approve → (Driver на смене) Claim → Start → Complete | Статус проходит draft→pending→awaiting_driver→scheduled→in_progress→completed; каждый переход виден всем ролям | Blocker `[test_full_workflow]` |
|1.2| Черновик заказа | Requester редактирует черновик (адрес/время) | Разрешено только в статусе DRAFT и только создателю; редактирование диспетчером → 403/404 | High `[test_draft_edit_only_by_creator]` |
|1.3| Заказ в статусе pending | Requester (автор) отклоняет (Reject) с указанием причины | Статус=rejected; причина + rejected_by записаны; заказ покидает очередь | High `[test_reject_by_author]` |
|1.4| Заказ в статусе awaiting | Dispatcher отклоняет (Reject) | Статус=rejected; наложение (overlay) демонтируется, чтобы прекратить авто-диспетчеризацию | High `[test_reject_by_dispatcher_on_awaiting]` |
|1.5| Заказ в статусе scheduled | Dispatcher отменяет (Cancel) с указанием причины | Статус=cancelled; временное окно водителя освобождается; смена возвращается в ONLINE | High `[test_cancel_frees_window]` |
|1.6| Черновик заказа | Requester удаляет (Delete) | 204; строка удалена. Удаление не-черновика → 400 | Med `[test_destroy_*]` |
|1.7| Несоответствие любой роли | Requester пытается Approve; не-создатель пытается Submit; не тот водитель пытается Complete | Все 403 | High `[test_requester_cannot_approve / submit_forbidden / only_assigned_driver_completes]` |
|1.8| Заказ в статусе completed/rejected/cancelled | Попытка Cancel / Reject / Extend | 400 INVALID_STATUS (терминальный статус) | Med `[test_cancel_rejects_terminal / reject_rejects_wrong_status]` |
|1.9| Заказ с активностью | Открыть журнал аудита (audit trail) | Записи created/sent/approved/accepted/completed/… с актором (actor) + меткой времени | Low `[test_activity_lists_the_audit_trail]` |

## 2. Диспетчер

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|2.1| Заказ в статусе scheduled на водителе | Dispatcher выполняет переназначение (Reassign) | Возврат в awaiting_driver; водитель снят + уведомлён; заказ снова входит в очередь | High `[test_reassign_by_dispatcher]` |
|2.2| Страница авто-диспетчеризации | Прочитать переключатель, включить (ON), выключить (OFF) | `effective` = env-переключатель И DB-переключатель; выполнять POST может только диспетчер (driver → 403) | High `[test_auto_dispatch_* / auto_dispatch_post_forbidden]` |
|2.3| Env `AUTO_DISPATCH_ENABLED=false` | Включить (ON) в UI | `effective=false` (env-аварийный выключатель (kill-switch) приоритетнее); воркер ничего не назначает | High `[test_auto_enabled_respects_env_kill_switch]` |
|2.4| Несколько активных заказов/водителей | Открыть доску автопарка/живую доску (fleet/live board) | Каждый активный заказ со своим живым маркером; терминальные заказы отсутствуют | Med |
|2.5| Воркер работает, переключатель выключен (OFF) в середине прохода | Наблюдать за текущим (in-flight) проходом | Уже начавшийся проход всё равно назначает; СЛЕДУЮЩИЙ проход ничего не делает | Med |

## 3. Водитель — смена (Р1)

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|3.1| Водитель с ≥1 назначенной машиной | Выйти на смену (выбрать машину) | Создана онлайн-смена; лента отфильтрована по типу этой машины | Blocker `[test_full_workflow]` |
|3.2| На смене | Выбрать машину, не назначенную вам / неактивную / на смене другого водителя | 403 / CAR_UNAVAILABLE / CAR_BUSY соответственно | High `[test_shift_rejects_*]` |
|3.3| На смене, нет активной поездки | Сменить машину | Старая смена завершается, новая ONLINE-смена на новой машине | Med `[test_switch_car_ends_old_and_starts_new]` |
|3.4| На смене, поездка в процессе | Сменить машину / Завершить смену | 400 DRIVER_BUSY (сначала завершите поездку) | High `[test_switch_blocked_mid_trip / end_shift_blocked_mid_trip]` |
|3.5| На смене, свободен | Завершить смену | ended_at установлен, статус OFFLINE; машина освобождена для других | Med `[test_end_shift_when_free_sets_offline]` |

## 4. Водитель — claim / trip-state

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|4.1| Заказ в статусе awaiting, водитель НЕ на смене | Claim | 400 NO_SHIFT | High `[test_claim_requires_active_shift]` |
|4.2| Тип машины смены ≠ типу заказа | Claim | 400 TYPE_MISMATCH | High `[test_claim_rejects_type_mismatch]` |
|4.3| Водитель держит заказ, пересекающийся с новым окном | Claim | 409 TIME_CONFLICT с деталями конфликтующего заказа | High `[test_overlapping_window_rejected]` |
|4.4| Два непересекающихся окна | Claim обоих | Оба в статусе scheduled (один водитель, несколько заказов) | High `[test_non_overlapping_windows_allowed]` |
|4.5| Заказ в статусе scheduled | Тапы по trip-state to_client → at_client → in_trip → at_destination → completed | Каждый тап продвигает на один допустимый шаг; пропуски отклоняются (INVALID_TRANSITION) | High `[test_can_transition_* / validate_*]` |
|4.6| Водитель уже движется по заказу A | Попытаться начать движение по заказу B (to_client/in_trip) | 400 ACTIVE_TRIP_EXISTS; *запаркованный* (waiting/at_destination) заказ НЕ блокирует | High `[test_blocks_second_moving_trip*]` |
|4.7| Заказ туда-обратно (has_return) | На месте назначения попытаться Complete до обратного плеча (return leg) | Заблокировано; нужно пройти at_destination→in_trip (возвращение) → обратно → Complete | Med `[test_validate_blocks_complete_before_return_leg]` |

## 5. Геозона (подтверждение прибытия) — нужен реальный девайс/симулятор GPS

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|5.1| Водитель >100 м от точки подачи, свежий GPS | Тап «Прибыл» (at_client) | 400 TOO_FAR (показывает расстояние) | High `[test_geofence_rejects_just_outside]` |
|5.2| Водитель ≤100 м, свежий GPS | Тап «Прибыл» | Разрешено | High `[test_geofence_passes_just_inside]` |
|5.3| Водитель в точке, фикс GPS старше >120 с | Тап «Прибыл» | 400 NO_FRESH_GPS | High `[test_geofence_rejects_a_stale_but_present_fix]` |
|5.4| То же, в точке НАЗНАЧЕНИЯ (включая обратное плечо) | Тап «Прибыл в место назначения» | Геозона проверяется относительно точки назначения / точки возврата | High `[test_geofence_at_destination_*]` |
|5.5| `CAR_ORDER_ARRIVAL_GEOFENCE_M=0` | Тап «Прибыл» из любой точки | Проверка геозоны пропущена (отключена) | Low `[test_geofence_disabled_when_radius_zero]` |

## 6. Живое отслеживание (live tracking) (реальный девайс + карта) — исследовательский

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|6.1| Поездка в процессе, клиент наблюдает | Водитель движется | Маркер + маршрут обновляются ~1 Гц на картах клиента и автопарка | High |
|6.2| Водитель запаркован (нет движения) | Простой 1–2 мин | «Соединение» остаётся живым (heartbeat запаркованного); маркер не дёргается (jitter) | High |
|6.3| Дрожание (jitter) GPS водителя <12 м | Стоять на месте | Маркер не дёргается (мёртвая зона, dead-zone) | Med |
|6.4| Водитель отклонился >80 м от маршрута | Свернуть не туда | Маршрут пересчитывается от новой позиции | Med |
|6.5| Одобренный заказ без водителя | Открыть трекер | Запланированный маршрут A→B отрисован (пины + линия) до появления любого водителя | Med |
|6.6| Переназначение / отклонение в середине поездки | Наблюдать карту клиента | Отслеживание чисто завершается («cancelled»); заказ нового водителя стартует с нуля | High |
|6.7| OSRM недоступен | Назначить + двигаться | Откат на прямую линию; уже отрисованный хороший дорожный маршрут НЕ перезаписывается | Med `[test_push_route_keeps_good_route*]` |

## 7. Планирование и продление

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|7.1| Заказ A 10:00–15:00 захвачен (claimed) | Claim B со стартом 15:20 (внутри 30-мин буфера) против 16:00 | 15:20 → 409; 16:00 → OK | High `[test_travel_buffer_enforced]` |
|7.2| Водитель запаркован на длительной съёмке (at_destination/waiting) | Claim заказа в промежутке внутри этого окна | Разрешено — время простоя свободно | High `[test_meta_conflict_parked_*]` |
|7.3| Активный заказ, водитель или диспетчер | Продлить («продлить») на N минут | Применяется всегда; предупреждает, если новый конец пересекается со следующим заказом | High `[test_extend_flags_conflict_with_next]` |
|7.4| Заказ создан без длительности | Продлить (Extend) | Работает (стартует с 0), без 400 | Med `[test_extend_with_no_prior_duration]` |
|7.5| Водитель на затянувшейся поездке | Проверить следующий заказ | Помечается at_risk / needs_reassign, как только прогнозируемый старт проходит latest_start | Med `[test_projected_start_pushes_past_an_overrunning_trip]` |

## 8. Устойчивость и конкурентность (только вручную / на staging)

> Автоматизированный набор тестов работает на SQLite, где `select_for_update()` — это **no-op**, поэтому
> кейсы с гонками ниже **не** покрыты «зелёными» тестами — проверяйте их на staging-окружении с
> Postgres и двумя параллельными клиентами. См. находки C1/C2/H1/H3 в `AUDIT.md`.

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|8.1| Один заказ в статусе awaiting | Два клиента одновременно выполняют Claim | Ровно один побеждает; другой получает чистую ошибку (не двойное назначение) | Blocker |
|8.2| Один свободный водитель, два готовых к выдаче заказа | Гонка авто-диспетчеризации и ручного claim | Водитель в итоге имеет ровно ОДИН активный заказ | Blocker |
|8.3| Одна машина, два водителя | Оба одновременно стартуют смену на ней | Один успешен; другой получает CAR_BUSY, а не 500 | High |
|8.4| Redis/слой каналов (channel layer) недоступен | Провести поездку | REST-вызовы всё ещё успешны; живые кадры теряются, но ничего не падает с 500 | High |
|8.5| Вышестоящий demo даёт 502/таймаут | Approve/Reject через хуки шлюза (gateway) | Нет рассогласования (split-brain) demo/overlay (оба движутся вместе или ни один) | High |
|8.6| Перезапуск воркера при ожидающих ASAP-заказах | Перезапустить `auto_dispatch` | Таймер ASAP «достаточно долго ждал» не должен сбрасываться бесконечно (известный пробел — M6) | Med |

## 9. Авторизация (AuthZ) / безопасность (запуск с `REQUIRE_OVERLAY_AUTH=True`)

| # | Pre | Steps | Expect | Sev |
|---|-----|-------|--------|-----|
|9.1| Токен обычного водителя | Вызвать reassign / meta DELETE / auto-dispatch POST | 403 (только для диспетчера) | High `[test_reassign_forbidden / meta_delete_forbidden / auto_dispatch_post_forbidden]` |
|9.2| Токен водителя A, в теле driver_id=B | Overlay-claim | Личность берётся из токена (A), тело игнорируется | High `[test_enforced_identity_comes_from_token_not_body]` |
|9.3| Токен водителя | GET my-overlay-orders?driver_id=other | Только собственные заказы (защита от IDOR); диспетчер может фильтровать по любому | High `[test_enforced_my_orders_ignores_query_driver_id]` |
|9.4| **Нет** токена | POST live-location / meta для произвольного id заказа | ⚠️ В настоящее время ПРИНИМАЕТСЯ (AllowAny) — см. AUDIT C3. Проверьте, приемлемо ли это для развёртывания | Blocker |
