# 11 — Order Templates (“заготовки”) on the create screen — Flutter

**Goal:** let a user save a recurring route (e.g. «Севимли → Сквер») once and re-apply it on every
new order instead of re-typing From/To, car type, duration and note. The web client
(`ark_yandex_front`) already ships this; this doc is the Flutter parity spec — same backend contract,
mirror the web behaviour.

> **Where this lives:** templates are a **local gateway overlay** (like `meta`), mounted *before* the
> demo proxy. The order itself is still created the normal way ([02-car-orders.md](02-car-orders.md))
> + meta saved ([03-scheduling-overlay.md](03-scheduling-overlay.md)). A template **never** touches
> demo — it is purely a form-prefill convenience stored on our gateway and shared across the team.

---

## 1. Backend contract

Base path is the usual mobile scheme: `{host}/{lang}/api/v1/...` (the `/<lang>/` segment is stripped
by the gateway — see [README](README.md)). Auth = the same `Bearer <access>` token as every other call.

| Method | Path | Effect |
|---|---|---|
| `GET` | `/car-orders/templates/` | list **all** templates (team-shared, ordered by `name`) |
| `POST` | `/car-orders/templates/` | create a template (sets `created_by_id` server-side) |
| `PATCH` | `/car-orders/templates/{id}/` | edit a template (partial) |
| `DELETE` | `/car-orders/templates/{id}/` | delete → `{ "ok": true }` (idempotent) |

- The list is **flat** (not paginated) — a JSON array of templates.
- Any authenticated user may create / edit / delete (it’s an internal team tool — no role gate).
  In dev `REQUIRE_OVERLAY_AUTH` is off, so the endpoints are open; in prod they require a logged-in
  user. There is **no per-user filtering** — everyone sees the same list.

### Template shape (response of GET / POST / PATCH)

```json
{
  "id": 7,
  "name": "Севимли → Сквер",
  "project_name": "Turandot Residences",
  "origin_lat": 41.311,
  "origin_lng": 69.279,
  "origin_label": "Севимли, Tashkent",
  "address": "Сквер, Amir Temur ave",
  "address_lat": 41.327,
  "address_lng": 69.281,
  "car_type_id": 4,
  "estimated_duration": 35,
  "service_time": 10,
  "note": "Pick up equipment",
  "created_by_id": 10,
  "created_at": "2026-06-19T11:10:00Z",
  "updated_at": "2026-06-19T11:10:00Z"
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | int | server-generated, read-only |
| `name` | string | **required**, ≤120 chars — the chip label |
| `project_name` | string | optional default order name (web prefills the name field from it) |
| `origin_lat` / `origin_lng` | float? | pickup coords (the **From** point), nullable |
| `origin_label` | string | pickup address text (the **From** label) |
| `address` | string | **destination** address text (the **To** label) — mirrors the order’s `address` |
| `address_lat` / `address_lng` | float? | destination coords (the **To** point), nullable |
| `car_type_id` | int? | demo `CarType.id` (a plain int, **not** a FK) — same value used in `meta`/create |
| `estimated_duration` | int? | minutes |
| `service_time` | int? | on-site minutes |
| `note` | string | order note |
| `created_by_id` | int? | demo user id, read-only (set on POST) |
| `created_at` / `updated_at` | ISO-8601 | read-only |

### Create / update body

Send only what you have — every field except `name` is optional and nullable; the server strips
nothing for you, so just omit empty keys. **Do not send** `id`, `created_by_id`, `created_at`,
`updated_at` (read-only). Validation: lat ∈ [-90, 90], lng ∈ [-180, 180].

```json
{
  "name": "Севимли → Сквер",
  "project_name": "Turandot Residences",
  "origin_lat": 41.311, "origin_lng": 69.279, "origin_label": "Севимли, Tashkent",
  "address": "Сквер, Amir Temur ave", "address_lat": 41.327, "address_lng": 69.281,
  "car_type_id": 4, "estimated_duration": 35, "service_time": 10,
  "note": "Pick up equipment"
}
```

> **A template carries NO date/time.** There is no `planned_datetime` field — pickup date and time are
> picked fresh on every order. When you *apply* a template, leave the schedule pickers empty.

---

## 2. Behaviour to mirror (from the web)

1. **Create-only.** The templates bar is shown (and the list fetched) **only when creating a new
   order**, never when editing. On mobile that’s the creation screen
   ([`car_order_creation_page.dart`](#4-ui-integration)); there is no “edit template-bar”.
2. **Apply = prefill, never the schedule.** Tapping a template fills From/To (label + coords), the
   order name (from `project_name`), the note, and carries `car_type_id` / `estimated_duration` /
   `service_time` forward to the ride-options step. Date and time stay empty.
3. **Save-as-template needs a complete route.** The “Save as template” action is blocked until both
   From and To are set — the route is the whole point. It opens a small dialog asking only for a
   **name** (prefilled with `"{From} → {To}"`, ≤120 chars), then POSTs the current draft.
4. **Delete is inline, no confirm.** Each chip has an `×`; tap → `DELETE`, then refresh the list.
5. **No permission gate in the UI** — the backend decides. Show the bar to anyone on the create screen.

---

## 3. Data + repository layer (Flutter)

### 3.1 Freezed model

New file: `lib/features/car_orders/domain/models/order_template_model/order_template_model.dart`
(follow the project’s `@freezed` + generated `fromJson`/`toJson` convention, like
`car_type_model.dart`). Field names map snake_case ⇄ camelCase via `@JsonKey`.

```dart
import 'package:freezed_annotation/freezed_annotation.dart';

part 'order_template_model.freezed.dart';
part 'order_template_model.g.dart';

@freezed
abstract class OrderTemplateModel with _$OrderTemplateModel {
  const factory OrderTemplateModel({
    int? id,
    required String name,
    @JsonKey(name: 'project_name') String? projectName,
    @JsonKey(name: 'origin_lat') double? originLat,
    @JsonKey(name: 'origin_lng') double? originLng,
    @JsonKey(name: 'origin_label') String? originLabel,
    String? address, // destination label
    @JsonKey(name: 'address_lat') double? addressLat,
    @JsonKey(name: 'address_lng') double? addressLng,
    @JsonKey(name: 'car_type_id') int? carTypeId,
    @JsonKey(name: 'estimated_duration') int? estimatedDuration,
    @JsonKey(name: 'service_time') int? serviceTime,
    String? note,
    @JsonKey(name: 'created_by_id') int? createdById,
  }) = _OrderTemplateModel;

  factory OrderTemplateModel.fromJson(Map<String, dynamic> json) =>
      _$OrderTemplateModelFromJson(json);
}
```

Then run:

```bash
dart run build_runner build --delete-conflicting-outputs
```

> When POSTing, send only the writable fields — drop `id`/`createdById` and any null keys (mirror the
> `..removeWhere((_, v) => v == null)` pattern already used in `OverlayRepositoryImpl.upsertMeta`).

### 3.2 Endpoint constants

Add to `lib/core/hosts/endpoints.dart` (next to `carOrderMeta`, etc.):

```dart
String get carOrderTemplates => '$baseUrl/$apiV1/car-orders/templates/';
String carOrderTemplateDetail(int id) =>
    '$baseUrl/$apiV1/car-orders/templates/$id/';
```

### 3.3 Repository methods

Add to the interface `lib/features/car_orders/domain/repositories/overlay_repository.dart`:

```dart
Future<List<OrderTemplateModel>> listTemplates();
Future<OrderTemplateModel> createTemplate(OrderTemplateModel template);
Future<void> deleteTemplate(int id);
```

Implement in `lib/features/car_orders/data/repositories/overlay_repository_impl.dart` (same `Dio`
client + `_asMap` helper already in that file; the GET returns a bare array, so map the list directly):

```dart
@override
Future<List<OrderTemplateModel>> listTemplates() async {
  final response = await client.get(endpoints.carOrderTemplates);
  final raw = response.data is List ? response.data as List : const [];
  return raw
      .map((e) => OrderTemplateModel.fromJson((e as Map).cast<String, dynamic>()))
      .toList();
}

@override
Future<OrderTemplateModel> createTemplate(OrderTemplateModel template) async {
  final body = template.toJson()
    ..remove('id')
    ..remove('created_by_id')
    ..removeWhere((_, value) => value == null);
  final response = await client.post(endpoints.carOrderTemplates, data: body);
  return OrderTemplateModel.fromJson(_asMap(response.data));
}

@override
Future<void> deleteTemplate(int id) async {
  await client.delete(endpoints.carOrderTemplateDetail(id));
}
```

### 3.4 Riverpod providers

New file: `lib/features/car_orders/presentation/providers/order_templates_provider.dart`
(`overlayRepositoryProvider` already exists in `overlay/overlay_providers.dart`):

```dart
/// The shared template list — fetched lazily on the create screen.
final orderTemplatesProvider =
    FutureProvider.autoDispose<List<OrderTemplateModel>>(
  (ref) => ref.read(overlayRepositoryProvider).listTemplates(),
);

/// Imperative actions (save / delete) that then refresh the list.
final orderTemplateActionsProvider = Provider.autoDispose((ref) {
  final repo = ref.read(overlayRepositoryProvider);
  return (
    save: (OrderTemplateModel t) async {
      final created = await repo.createTemplate(t);
      ref.invalidate(orderTemplatesProvider);
      return created;
    },
    remove: (int id) async {
      await repo.deleteTemplate(id);
      ref.invalidate(orderTemplatesProvider);
    },
  );
});
```

---

## 4. UI integration — the create screen

File: `lib/features/car_orders/car_order_request/presentation/car_order_creation_page.dart`.
The bottom-sheet `SingleChildScrollView` already stacks: grab-handle → title → `_RouteRow` (From/To +
swap) → name field → schedule toggle → date/time → note → “Next”.

**Insert a horizontal templates bar between the `_RouteRow` and the name field.** Watch
`orderTemplatesProvider`; render a `SizedBox(height: ~56)` with a horizontal `ListView` of chips, plus
a trailing “＋ Save as template” chip. Hide the whole bar if the screen is in any edit mode (this
screen is create-only today, so simply always show it on creation — there is no edit variant).

```dart
final templates = ref.watch(orderTemplatesProvider);
// inside the sheet column, right after _RouteRow:
templates.maybeWhen(
  data: (list) => _TemplatesBar(
    templates: list,
    onApply: _applyTemplate,
    onDelete: (id) => ref.read(orderTemplateActionsProvider).remove(id),
    onSaveAs: _openSaveTemplateDialog,
  ),
  orElse: () => const SizedBox.shrink(),
);
```

### 4.1 Apply a template

Push the template into `carOrderDraftProvider` (see
`car_order_request/presentation/providers/car_order_draft_provider.dart`). The draft’s `setFromLocation`
/ `setToLocation` take an address + optional `GeoPoint` (`({double lat, double lng})`):

```dart
void _applyTemplate(OrderTemplateModel t) {
  final draft = ref.read(carOrderDraftProvider.notifier);

  draft.setFromLocation(
    t.originLabel ?? '',
    (t.originLat != null && t.originLng != null)
        ? (lat: t.originLat!, lng: t.originLng!)
        : null,
  );
  draft.setToLocation(
    t.address ?? '',
    (t.addressLat != null && t.addressLng != null)
        ? (lat: t.addressLat!, lng: t.addressLng!)
        : null,
  );
  draft.setName(t.projectName ?? t.name);
  draft.setNote(t.note ?? '');

  // Keep the controllers in sync with the draft we just set:
  _nameController.text = t.projectName ?? t.name;
  _noteController.text = t.note ?? '';

  // DO NOT touch the schedule — date/time stay empty on purpose.
}
```

> **Carry car type + duration forward.** The create screen (S2) does **not** pick the car type — that
> happens on the next screen, `ride_options_page.dart` (S3). To honour `car_type_id` /
> `estimated_duration` / `service_time` from the template, extend `CarOrderDraft` with three optional
> fields (`carTypeId`, `estimatedDuration`, `serviceTime`) + matching `copyWith` setters, set them in
> `_applyTemplate`, and have the ride-options page preselect the car type from the draft (mirrors the
> web’s `preserved.current`). If you ship a v1 without this, the route + name + note still prefill —
> just the car type is chosen manually downstream. Note this gap in the PR.

### 4.2 Save as template

Gate on a complete route, prefill the name with `"{From} → {To}"`, ask only for the name:

```dart
Future<void> _openSaveTemplateDialog() async {
  final draft = ref.read(carOrderDraftProvider);
  if (draft.fromPoint == null || draft.toPoint == null) {
    // toast: "Set both From and To first — the route is what we save."
    return;
  }
  final suggested = (draft.from.isNotEmpty && draft.to.isNotEmpty)
      ? '${draft.from} → ${draft.to}'
      : draft.name;

  final name = await showDialog<String>(/* TextField, maxLength: 120, prefilled `suggested` */);
  if (name == null || name.trim().isEmpty) return;

  await ref.read(orderTemplateActionsProvider).save(
        OrderTemplateModel(
          name: name.trim(),
          projectName: draft.name,
          originLat: draft.fromPoint!.lat,
          originLng: draft.fromPoint!.lng,
          originLabel: draft.from,
          address: draft.to,
          addressLat: draft.toPoint!.lat,
          addressLng: draft.toPoint!.lng,
          // carTypeId/estimatedDuration/serviceTime: from the extended draft, if added
          note: draft.note,
        ),
      );
  // toast: "Template saved"
}
```

Dialog copy (match the web): *“We’ll save the route, car type, duration and note. You’ll pick the date
and time again on every order.”*

### 4.3 Delete

Each chip shows an `×`; tap → `orderTemplateActionsProvider.remove(id)` (no confirm dialog — the web
deletes immediately and refreshes).

---

## 5. Checklist

- [ ] `OrderTemplateModel` freezed model + `build_runner` run.
- [ ] `endpoints.dart`: `carOrderTemplates` + `carOrderTemplateDetail(id)`.
- [ ] `OverlayRepository` (+ impl): `listTemplates` / `createTemplate` / `deleteTemplate`.
- [ ] `orderTemplatesProvider` + `orderTemplateActionsProvider`.
- [ ] `_TemplatesBar` between `_RouteRow` and the name field on the create screen.
- [ ] Apply → prefill From/To/name/note, **leave schedule empty**; sync the text controllers.
- [ ] Save-as-template dialog gated on a complete route; name ≤120 chars.
- [ ] Inline delete + list refresh.
- [ ] *(optional v1+)* extend `CarOrderDraft` to carry `carTypeId`/`estimatedDuration`/`serviceTime`
      into the ride-options step.
- [ ] `flutter analyze` clean.

---

### Reference

- Web implementation (the source of truth): `ark_yandex_front` →
  `src/pages/car-orders/CarOrderFormPage.tsx` (templates bar + apply + save dialog),
  `src/api/endpoints/carOrders.ts` (`listTemplates`/`createTemplate`/`updateTemplate`/`deleteTemplate`).
- Backend: `car_orders/models.py` (`CarOrderTemplate`), `car_orders/serializers.py`
  (`CarOrderTemplateSerializer`), `car_orders/views.py` (`CarOrderTemplatesView` /
  `CarOrderTemplateDetailView`), routes in `config/urls.py` (mounted before the gateway catch-all).
- Related mobile docs: [02-car-orders.md](02-car-orders.md) (create order),
  [03-scheduling-overlay.md](03-scheduling-overlay.md) (`meta` — where coords/duration are saved).
</content>
</invoke>
