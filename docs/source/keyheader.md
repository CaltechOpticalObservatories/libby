# Keyheader

Keyheader is a libby daemon that captures a snapshot of instrument state at a
moment and returns it as FITS header cards. A requesting daemon, usually a
camera daemon, asks for a named **segment** at a time such as the start of an
exposure, and adds the cards it gets back to its image header.

The values come from the database the {doc}`keygrabber` fills, so keyheader
never talks to a hardware daemon and a slow one cannot delay a header.
InfluxDB 2.x is the only backend today:

```bash
pip install libby[influxdb]
```

## Running it

```bash
keyheader -c /etc/hispec/keyheader.yaml
```

Like the keygrabber, it is an ordinary `LibbyDaemon`: `SIGTERM` or the
`shutdown` keyword stops it, and it logs to stderr unless the config has a
`logging` section.

## Config

A keyheader config is an ordinary daemon config plus `source`, `defaults` and
`segments`. A fuller, commented example ships with the package as
`libby/keyheader/keyheader.example.yaml`.

```yaml
peer_id: keyheader
group_id: hispec
transport: rabbitmq
rabbitmq_url: amqp://localhost

source:
  type: influxdb
  url: http://influx.hispec:8086
  org: hispec
  bucket: telemetry
  token_env: HISPEC_INFLUX_TOKEN

workers: 4

defaults:
  mode: nearest
  window_s: 300

segments:
  - name: EXP-BEGIN
    date_keyword: DATE-BEG
    cards:
      - fits: ADCPOS1
        keyword: hsfei.adc.positionvalue1
        type: float
        format: ".3f"
        comment: "ADC prism 1 position [deg]"
        mode: interpolate
      - fits: ADCMOVE
        keyword: hsfei.adc.ismoving
        type: bool
        comment: "ADC in motion"

  - name: EXP-END
    date_keyword: DATE-END
    defaults:
      mode: prior
    cards:
      - fits: ADCPOS1E
        keyword: hsfei.adc.positionvalue1
        type: float
        format: ".3f"
        comment: "ADC prism 1 position at end [deg]"
```

### source

Validated exactly like the keygrabber's `sink`: `type: influxdb`, and the token
is read from the environment variable `token_env` names. A `token` key in the
file is rejected.

### segments

`segments` is a list, and so is each segment's `cards`. They are not mappings
on purpose: YAML keeps only the last of two identical keys without warning,
so a duplicated FITS keyword written as a key would disappear rather than be
reported. A mapping is refused with an error that says so.

A segment has:

- `name`: letters, digits, `_`, `-` or `.`, at most 40 characters. Requests
  use this name, and it starts the segment's COMMENT card.
- `date_keyword` (optional): one of `DATE-OBS`, `DATE-BEG`, `DATE-AVG` or
  `DATE-END`. These are the FITS standard's own time keywords. The segment
  writes its timestamp there as well as in the COMMENT.
- `date_comment` (optional): the comment for that card.
- `defaults` (optional): `mode` and `window_s` for this segment's cards. These
  override the top-level `defaults`.
- `cards`: the mapped keywords, in the order they appear in the header.

A card has:

| Field | Required | Meaning |
|---|---|---|
| `fits` | yes | FITS keyword: 1-8 characters of `A-Z`, `0-9`, `-`, `_` |
| `keyword` | yes | Daemon keyword, `<group>.<daemon>.<keyword>` |
| `type` | yes | `float`, `int`, `bool` or `str` |
| `format` | no | Python format spec, e.g. `.3f`, `.2e`, `.20` |
| `comment` | no | Card comment, at most 47 printable ASCII characters |
| `mode` | no | `interpolate`, `nearest`, `prior` or `subsequent` |
| `window_s` | no | How far from the moment to look for a sample |

### Validation

Config load rejects:

- a FITS keyword that breaks the naming rules
- a keyword the FITS 4.0 standard reserves. This covers the structural ones
  (`SIMPLE`, `NAXISn`, `END`, ...), commentary (`COMMENT`, `HISTORY`,
  `CONTINUE`), the general descriptive ones (`OBJECT`, `TELESCOP`, `DATE`,
  ...), and the WCS and time keywords. Keyheader writes COMMENT itself.
- a FITS keyword used twice, either within one segment or across segments.
  The requesting daemon merges segments into one header, and FITS forbids a
  repeated keyword, so the error names both segments rather than renaming
  either.
- a repeated segment name
- `interpolate` on a `bool` or `str` card
- a `format` the type cannot take, or a numeric format whose output does not
  read back as a number (e.g. `,.2f`)
- a comment longer than 47 characters, the space left beside a fixed-format
  value

A bad config stops the daemon at startup, and a bad file given to `reload`
leaves the running segments in place.

## Acquisition modes

Every card is looked up independently, within `window_s` of the requested
moment:

- `prior`: the latest sample at or before the moment
- `subsequent`: the earliest sample at or after the moment
- `nearest`: whichever of those two is closer. A tie goes to the earlier
  sample, which was already true at the moment.
- `interpolate`: linear interpolation between the samples either side. When
  only one side exists, it uses that value.

That fallback matters at the end of an exposure. A request made then normally
arrives before the keygrabber has stored the next sample, and most values do
not change during an exposure, so the last stored value stands in. A
`subsequent` card has no such fallback and is written blank.

`window_s` should exceed the keygrabber's interval for that keyword, including
its backoff, or a quiet keyword will come back blank.

## Requesting a segment

The service key is `keyheader.segment`:

```python
reply = libby.rpc("hispec.keyheader", "keyheader.segment",
                  {"segment": "EXP-BEGIN", "time": "2026-11-11T08:15:02.123Z"})
```

`time` is ISO 8601. A time with no offset is taken as UTC. The reply is:

```json
{
  "ok": true,
  "segment": "EXP-BEGIN",
  "time": "2026-11-11T08:15:02.123",
  "cards": [
    {"keyword": "COMMENT",  "value": "EXP-BEGIN 2026-11-11T08:15:02.123", "comment": ""},
    {"keyword": "DATE-BEG", "value": "2026-11-11T08:15:02.123", "comment": "UTC time of this segment"},
    {"keyword": "ADCPOS1",  "value": 12.346, "comment": "ADC prism 1 position [deg]"},
    {"keyword": "ADCMOVE",  "value": false,  "comment": "ADC in motion"}
  ]
}
```

An unknown segment or a malformed request comes back as `ok: false` with an
`error`.

### Adding the cards with astropy

Keyheader does not write card images. The requesting daemon passes each entry
to astropy as a tuple:

```python
cards = [(c["keyword"], c["value"], c["comment"]) for c in reply["cards"]]
header.extend(cards, end=True)
```

Use `end=True`, or build a fresh `fits.Header(cards)`. Plain
`header.extend(cards)` inserts keyword cards ahead of trailing commentary
cards, which moves the segment's COMMENT below the cards it introduces.

The requesting daemon is still responsible for checking the final header
against the FITS standard. For example, it should not add a second segment
that names the same keywords.

### Values and formats

A value is typed JSON, and astropy decides the column layout. `format`
therefore sets precision rather than layout: the value is formatted and then
parsed back, so `.3f` turns `12.34567` into `12.346`. Other conversions:

- An `int` card rounds, because InfluxDB stores every integer as a float.
- A `bool` card accepts a stored `0` or `1`.
- A `str` format can truncate, e.g. `.20`.

### Blank cards

A card is never left out. When there is no value, it is written with a FITS
undefined value (`null` in the reply, which astropy writes as a blank value),
and its comment starts with the reason:

| Reason | Cause |
|---|---|
| `no sample`, `no prior sample`, `no later sample` | Nothing stored in the window |
| `read failed` | The database query failed |
| `not a float`, `not a bool`, ... | The stored value cannot be the configured type |
| `not finite` | NaN or infinity, which FITS cannot represent |
| `not numeric` | Interpolation between non-numbers |

The reason and the original comment together are truncated to 47 characters.
One failed card never fails the request: losing one value should not cost the
exposure its whole header.

### Latency

The cards in a segment are read in parallel on `workers` threads. A request
therefore costs about one database round trip, plus queueing when a segment
has many more cards than workers. The request is handled on the transport's
receive thread, so it holds up other requests to keyheader for that long.
Give the RPC a TTL that covers the database's `timeout_ms`.

## Control keywords

| Keyword | Type | Access | Meaning |
|---|---|---|---|
| `isconnected` | bool | R | Last source read succeeded |
| `requests` | int | R | Segment requests answered since start |
| `readerrors` | int | R | Source reads that failed |
| `blankcards` | int | R | Cards answered with an undefined value |
| `segments` | string | R | Configured segment names, comma separated |
| `reload` | trigger | W | Re-read the config file and apply its segments |
| `shutdown` | trigger | W | Gracefully stop the daemon |

`reload` can add, remove or change segments without a restart, because
segments are not keywords. A change to `source` or `workers` is refused with a
request to restart instead. A request already in progress finishes with the
segments it started with.