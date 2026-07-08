# TODO — Tests Twilio (conversation téléphonique)

Périmètre : `tests/twilio/` (suite pytest mockée + scripts manuels de diagnostic).
Design de référence : `docs/superpowers/specs/2026-07-04-twilio-telephony-e2e-tests-design.md`.

## Fait

- [x] 4 fichiers pytest mockés (31 tests, zéro appel réseau) : `test_phone_call_tool.py`,
      `test_voice_sip_context.py`, `test_voice_providers.py`, `test_voice_tools_bridge.py`.
- [x] Bug de production corrigé : `logger.info(msg, numero=..., ...)` sur un
      `logging.getLogger` standard dans `agent.py` (faisait planter tout appel entrant).
- [x] `test_numero_invalide_rejete` corrigé — un refactor automatique avait remplacé le
      numéro volontairement invalide par `_NUMERO` (valide), vidant le test de son sens.
- [x] `twilio_call.py` / `twilio_sms.py` lisent `TWILIO_FROM_NUMBER` /
      `TWILIO_TEST_TO_NUMBER` depuis `.env` au lieu de numéros en dur.
- [x] Chemin `.env` résolu relativement au script (`Path(__file__).resolve().parent...`)
      dans les 3 scripts manuels — fonctionne peu importe le cwd d'exécution.

## Reste à faire

- [ ] **Volet 2 du design (jamais écrit)** : `scripts/validation/telephony_real_e2e.py`,
      script de diagnostic live sur le modèle de `scripts/validation/smoke_runtime.py`
      (config présente → LiveKit joignable → Twilio joignable → appel réel optionnel via
      `--real --to`). Doit remplacer le besoin de lancer `twilio_call.py` à la main pour
      vérifier la chaîne bout en bout.
- [ ] Revue de `twilio_messenger.py` : mêmes garanties que `twilio_call.py`/`twilio_sms.py`
      (déjà lit page_id/to_psid depuis `.env` — vérifier qu'aucune valeur n'y est en dur).
- [ ] Décider si `tests/twilio/twilio_*.py` doivent être exclus explicitement de la
      découverte pytest (actuellement non collectés car le nom ne matche pas
      `test_*.py`/`*_test.py` — comportement implicite, pas garanti si la config
      `[tool.pytest.ini_options]` change un jour).
- [ ] Les 31 échecs pré-existants ailleurs dans `tests/` (isolation entre tests, hors
      périmètre Twilio) restent à investiguer séparément — non liés à ce travail.
