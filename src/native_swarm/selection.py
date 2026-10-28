"""Read-only catalog ranking before any address acquisition."""


def ordered_functions(call, bands, priority, band_for_size):
    rows = []
    for band in bands:
        cursor = None
        seen = set()
        while True:
            query = {
                "band": band,
                "order": "returned" if priority == "recently returned" else "size",
                "limit": 500,
                "exclude_reported": True,
            }
            if cursor is not None:
                query["cursor"] = cursor
            page = call("list_functions", query)
            if not isinstance(page, dict) or not isinstance(
                page.get("functions"), list
            ):
                raise ValueError(
                    "Function catalog returned an unsupported response; no lease acquired."
                )
            rows.extend(
                r for r in page["functions"] if band_for_size(r["size"]) in bands
            )
            cursor = page.get("next")
            if not cursor:
                break
            if str(cursor) in seen:
                raise ValueError(
                    "Function catalog repeated its cursor; no lease acquired."
                )
            seen.add(str(cursor))
    unique = {str(r["address"]): r for r in rows}
    if priority == "recently returned":
        # Preserve server order within equal timestamps; dated returns precede
        # never-returned entries. ISO timestamps use the service's UTC format.
        return sorted(
            unique.values(), key=lambda r: r.get("back_in_pool_at") or "", reverse=True
        )
    return sorted(
        unique.values(),
        key=lambda r: (r["size"], str(r["address"])),
        reverse=priority == "largest",
    )
