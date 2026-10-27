# isoparse interprets fractional seconds with the wrong scale

isoparse('2024-03-05T06:07:08.125') gives 125 microseconds, but .125 seconds means 125000 microseconds. The function also rejects fractions longer than six digits instead of accepting their microsecond precision.

For naive date-time strings with a decimal point followed by digits after the seconds, interpret the fraction as a fraction of one second: right-pad shorter fractions to six decimal places and truncate excess digits without rounding or carrying into the next second. Preserve date-only and whole-second inputs, six-digit fractions, and datetime.isoformat round trips. Invalid calendar dates should still be rejected. Timezone offsets, Z suffixes, alternate fraction separators and general ISO8601 expansion are outside this task. Change implementation only; do not modify tests or metadata.
