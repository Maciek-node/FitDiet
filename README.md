# Dobry Talerz

## Uruchomienie serwera

Wymagany jest Python 3.10 lub nowszy. W katalogu projektu uruchom:

```sh
python3 server.py --host 0.0.0.0 --port 8765
```

Jeśli port `8765` jest już zajęty przez poprzedni serwer statyczny, zatrzymaj go i uruchom ten serwer ponownie. Aplikacja będzie dostępna z innych urządzeń w tej samej sieci pod adresem `http://ADRES-IP-KOMPUTERA:8765/`.

## Konta i kopie zapasowe

Konta, sesje i dane użytkowników są przechowywane w SQLite w ukrytym katalogu `.fitdiet-data/`. Katalog jest wyłączony z repozytorium i zabezpieczony uprawnieniami tylko dla właściciela procesu. Kopię zapasową wykonuj po zatrzymaniu serwera, kopiując cały katalog `.fitdiet-data/` do bezpiecznego miejsca.

Przy logowaniu aplikacja wczytuje stan konta z serwera. Każda późniejsza zmiana jest zapisywana na serwerze.

## Prywatny dostęp jednego użytkownika

Rejestracja jest wyłączona w interfejsie i API. Dla serwera publicznego ustaw zmienne środowiskowe `FITDIET_SINGLE_USER_EMAIL` i `FITDIET_SINGLE_USER_PASSWORD`; podczas startu serwer utworzy lub zaktualizuje konto właściciela. Hasło musi mieć 10–128 znaków. Zmiana hasła unieważnia istniejące sesje. Nie wpisuj hasła do repozytorium ani pliku `render.yaml`.

Gdy zmienne nie są ustawione, serwer dopuszcza logowanie tylko wtedy, gdy w bazie istnieje dokładnie jedno konto. Samodzielna rejestracja pozostaje wyłączona.

## Wystawienie poza sieć lokalną

Do publicznego wdrożenia użyj HTTPS przez reverse proxy i ustaw `FITDIET_COOKIE_SECURE=1`. Nie wystawiaj serwera z domyślnym HTTP bezpośrednio do Internetu. Hasła są przechowywane jako skróty PBKDF2; serwer nie wysyła linków weryfikacyjnych ani do odzyskiwania hasła, więc przed publicznym wdrożeniem trzeba dodać obsługę odzyskiwania konta i skonfigurować kopie zapasowe.
