                    result["portfolio_name"] = portfolio_name

                    report_id = (
                        details.get("ownerStatementReportID")
                        or details.get("ownerStatementReportId")
                    )

                    if not report_id:
                        raise ValueError(
                            "Owner Statement report ID missing."
                        )

                    if not portfolio_name:
                        raise ValueError(
                            "Portfolio name missing."
                        )

                    name_key = normalize_name(portfolio_name)

                    matching_records = hubspot_index.get(
                        name_key,
                        []
                    )

                    if len(matching_records) == 0:
                        result["status"] = "SKIPPED - NO HUBSPOT MATCH"
                        counters["skipped"] += 1
                        log.warning(
                            "No HubSpot match for %s.",
                            portfolio_name
                        )
                        results.append(result)
                        continue

                    if len(matching_records) > 1:
                        result["status"] = "SKIPPED - DUPLICATE HUBSPOT MATCH"
                        counters["skipped"] += 1
                        log.warning(
                            "Multiple HubSpot matches for %s.",
                            portfolio_name
                        )
                        results.append(result)
                        continue

                    html = retrieve_statement_html(
                        page,
                        portfolio_id,
                        report_id
                    )

                    balance = extract_ending_balance(html)

                    record_id = matching_records[0]["id"]

                    result["ending_balance"] = format(
                        balance,
                        ".2f"
                    )

                    result["hubspot_record_id"] = record_id

                    update_hubspot_balance(
                        record_id,
                        balance
                    )

                    if DRY_RUN:
                        result["status"] = "DRY RUN - VERIFIED"
                        counters["dry_run"] += 1
                    else:
                        result["status"] = "UPDATED"
                        counters["updated"] += 1

                    log.info(
                        "%s | Ending Balance: %s",
                        portfolio_name,
                        balance
                    )

                except Exception as exc:
                    result["status"] = f"FAILED - {str(exc)}"
                    counters["failed"] += 1

                    log.exception(
                        "Portfolio processing failed: %s",
                        portfolio_name
                    )

                results.append(result)

        finally:
            context.close()
            browser.close()

    # Save audit report.
    with open(
        OUTPUT_CSV,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as csv_file:

        fieldnames = [
            "portfolio_id",
            "portfolio_name",
            "ending_balance",
            "hubspot_record_id",
            "status"
        ]

        writer = csv.DictWriter(
            csv_file,
            fieldnames=fieldnames
        )

        writer.writeheader()
        writer.writerows(results)

    log.info("=" * 55)
    log.info("SYNCHRONIZATION SUMMARY")
    log.info("=" * 55)

    for key, value in counters.items():
        log.info("%s: %s", key.upper(), value)

    log.info("Audit file created: %s", OUTPUT_CSV)

    # Fail the workflow if any portfolio failed.
