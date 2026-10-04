from incident_management import load_event_log


def main() -> None:
    event_log = load_event_log()
    print(f"events={len(event_log):,}")
    print(f"cases={event_log['case_id'].nunique():,}")
    print(f"activities={event_log['event'].nunique():,}")
    print(f"columns={','.join(event_log.columns)}")


if __name__ == "__main__":
    main()