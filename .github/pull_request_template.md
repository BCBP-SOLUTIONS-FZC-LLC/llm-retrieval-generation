## Description
Provide a clear description of the changes.

---

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Refactor
- [ ] Documentation
- [ ] Test
- [ ] Breaking change
- [ ] New migration
- [ ] Event contract change (`schemas/events/`)

---

## Testing
- [ ] Unit / usecase / api tests added/updated (`make test-unit`)
- [ ] Integration tests added/updated (`make test-integration`)
- [ ] Manual testing performed (if required)

---

## Checklist

### Code Quality
- [ ] Formatted (`make fmt-check`), linted (`make lint`), type-checked (`make typecheck`)
- [ ] No debug logs / commented-out code
- [ ] No secrets or DSNs hardcoded
- [ ] `CHANGELOG.md` updated (src/, migrations/, schemas/ changes)

### Event Schemas
*Complete only when `schemas/events/` changed.*
- [ ] `"additionalProperties": false` set on every modified or new schema
- [ ] Consumers updated before the producer emits the change
- [ ] No semantic drift in existing fields (range, units, encoding, meaning)

### Database / Migrations
- [ ] New migrations have matching `.up.sql` and `.down.sql`
- [ ] Down migration correctly reverses the up migration
- [ ] RLS policies tested with `FORCE ROW LEVEL SECURITY`

### Documentation
- [ ] Docs / LLD updated if layering, flows, or invariants changed

---

## Related Issue
Closes #<issue-id>

---

## Deployment Notes
Anything operators need to know (migration steps, new env vars, breaking event changes).
