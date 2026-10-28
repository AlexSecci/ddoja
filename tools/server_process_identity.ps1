# ConvertFrom-Json returns DateTime in newer PowerShell and strings in Windows
# PowerShell. Parsing a DateTime again loses its Kind through string conversion.
function ConvertTo-DashboardUtc {
    param([Parameter(Mandatory)]$Value)
    if ($Value -is [DateTimeOffset]) { return $Value.UtcDateTime }
    if ($Value -is [DateTime]) {
        if ($Value.Kind -eq [DateTimeKind]::Unspecified) {
            throw 'Saved dashboard timestamp has no timezone. No process was stopped.'
        }
        return $Value.ToUniversalTime()
    }
    if ($Value -isnot [string] -or $Value -notmatch '(?:[zZ]|[+-]\d{2}:\d{2})$') {
        throw 'Saved dashboard timestamp has no timezone. No process was stopped.'
    }
    return [DateTimeOffset]::Parse($Value, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime
}
