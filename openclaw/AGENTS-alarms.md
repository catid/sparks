## Cerberus timers and alarms

- Use `timer_set` for countdowns after converting the requested duration to an
  exact whole number of seconds.
- Use `alarm_set` for clock alarms. Pass a future ISO 8601 timestamp with an
  explicit UTC offset, and clarify AM or PM when the request is ambiguous.
- Use `alarms_list` before cancelling when the user has not supplied an exact
  alarm ID. Use `alarm_cancel` for a pending timer or alarm.
- Use `alarm_dismiss` when the user asks to stop a timer or alarm that is
  currently ringing. Omitting its ID stops all currently ringing alarms.
- Confirm a set, cancel, or dismiss action only after its tool succeeds. State
  the returned local due time concisely when setting one.
