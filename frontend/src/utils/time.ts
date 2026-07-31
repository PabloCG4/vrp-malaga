/** Workday clock starts at 08:00; every `*_seconds` field is an offset from that instant. */
const WORKDAY_START_HOUR = 8

/** Simulated workday length in minutes (08:00–16:00). */
export const WORKDAY_DURATION_MINUTES = 480

/** Format a `*_seconds` offset from workday start (08:00) as a zero-padded `HH:MM` string. */
export function formatWorkdaySeconds(seconds: number): string {
  const totalMinutes = Math.floor(seconds / 60)
  const hours = WORKDAY_START_HOUR + Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}`
}

/** Format a simulated minute offset from workday start (08:00) as a zero-padded `HH:MM` string. */
export function formatWorkdayMinutes(minute: number): string {
  return formatWorkdaySeconds(minute * 60)
}
