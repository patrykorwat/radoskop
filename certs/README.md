# certs/ — uzupełnienia łańcuchów CA

Pliki PEM tu to **certyfikaty pośrednie (intermediate)** serwerów, które nie
wysyłają pełnego łańcucha w TLS handshake. Dodane do zaufanych przez
scripts/ca_bundle.py (SSL_CERT_FILE). WERYFIKACJA przed dodaniem:
  openssl verify -CAfile <root-system> -untrusted <intermediate> <leaf> == OK

- certum-dv-tls-g2-r39-ca.pem — Certum DV TLS G2 R39 (bip.um.wroc.pl wysyła
  tylko certyfikat właściwy; kod 21 'unable to verify the first certificate').
  Źródło: http://certumdvtlsg2r39ca.repository.certum.pl/certumdvtlsg2r39ca.cer
  Dodane 2026-09-07 (run pipeline: wroclaw CERTIFICATE_VERIFY_FAILED).
