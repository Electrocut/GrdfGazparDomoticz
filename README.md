# GrdfGazpar : plugin Domoticz pour les compteurs gaz GRDF Gazpar

Ce plugin Python pour [Domoticz](https://www.domoticz.com) récupère automatiquement les consommations
journalières de gaz sur [monespace.grdf.fr](https://monespace.grdf.fr). Il les écrit ensuite dans un
périphérique Domoticz existant de type *Managed Counter* (Gaz).

## Fonctionnalités

- **Connexion automatique :** le plugin se connecte à Mon Espace GRDF via Okta (`connexion.grdf.fr`), sans captcha et sans Selenium.
- **Pas de doublon :** il n'interroge GRDF que s'il manque des journées dans le compteur.
- **Historique conservé :** l'index cumulé repart du dernier jour présent dans le périphérique, sans trou ni saut.
- **Données non publiées :** les journées que GRDF n'a pas encore mesurées sont ignorées, puis reprises au passage suivant.
- **Fonctionnement en arrière-plan :** la connexion à GRDF tourne dans un thread séparé et ne bloque pas Domoticz.
- **Erreurs claires dans le log :** mot de passe refusé, double authentification demandée, API indisponible…
- **Dépendance unique :** `requests`.

## Prérequis

- **Domoticz :** version 2023 ou plus récente, avec le support des plugins Python 3.
- **Bibliothèque Python :** `requests`, à installer si besoin avec `sudo apt install python3-requests`.
- **Compte GRDF :** un compte [Mon Espace GRDF](https://monespace.grdf.fr), avec le consentement « données quotidiennes » activé pour votre compteur. Sans lui, GRDF ne fournit que des relevés mensuels.
- **Numéro PCE :** il figure sur votre facture ou dans votre espace GRDF.
- **API locale :** Domoticz doit accepter les appels à son API depuis `127.0.0.1` sans authentification. C'est le cas par défaut : `127.0.0.1` fait partie des réseaux de confiance (*Paramètres → Sécurité*).

## Installation

```bash
cd ~/domoticz/plugins
git clone https://github.com/Electrocut/GrdfGazparDomoticz.git
sudo systemctl restart domoticz
```

Pour mettre à jour le plugin : `cd ~/domoticz/plugins/GrdfGazparDomoticz && git pull`, puis redémarrez Domoticz.

## Configuration

1. **Créez le périphérique compteur**, si vous n'en avez pas encore :
   - Dans *Configuration → Matériel*, ajoutez un matériel de type *Dummy*.
   - Cliquez sur *Créer capteurs virtuels* et choisissez le type **Managed Counter**.
   - Dans *Utilitaires → Modifier*, réglez le type de compteur sur **Gaz**.
   - Notez son **IDX**.
2. **Ajoutez le matériel** dans *Configuration → Matériel*, type **GRDF Gazpar (monespace.grdf.fr)** :

| Paramètre | Description |
|---|---|
| E-mail / Mot de passe GRDF | Identifiants de votre compte **Mon Espace GRDF** |
| Numéro PCE | Identifiant de votre point de comptage (14 chiffres) |
| IDX du périphérique compteur gaz | IDX du *Managed Counter* créé à l'étape 1 |
| Port HTTP Domoticz | Port local de l'interface web (8080 par défaut) |
| Rattrapage max (jours) | Nombre maximal de jours récupérés en cas de retard, y compris à la première exécution (30 par défaut, 365 au maximum) |
| Intervalle d'interrogation (heures) | Délai entre deux interrogations de GRDF (6 par défaut) |
| Debug | Journalisation détaillée |

La première interrogation a lieu environ 30 secondes après le démarrage du plugin.

## Données enregistrées

Pour chaque journée gazière (de 6 h à 6 h le lendemain), le plugin enregistre :

- **valeur du jour** : l'énergie consommée, `energieConsomme` en kWh × 1000, soit des **Wh** ;
- **index** : le dernier index connu du périphérique, augmenté de la valeur du jour.

C'est le même format que le script `gazpar.py` / `gazpar-man.py` de
[DomoticzGazpar](https://github.com/Scrat95220/DomoticzGazpar). Vous pouvez donc remplacer ce script
par le plugin sans perdre l'historique.

Pour afficher le compteur en m³ dans Domoticz, réglez le diviseur du périphérique sur environ
11 000 à 11 300 Wh/m³. Le coefficient de conversion exact est indiqué sur vos factures.

GRDF publie la journée J au plus tôt le lendemain, parfois avec un ou deux jours de retard.

## Dépannage

| Message dans le log | Cause probable |
|---|---|
| `mot de passe GRDF refusé : HTTP 401` | Identifiants incorrects. Utilisez ceux de **Mon Espace GRDF**, pas ceux d'un autre site GRDF. |
| `connexion GRDF incomplète (MFA demandé ?)` | Votre compte exige une double authentification, que le plugin ne gère pas. |
| `stateToken introuvable` | La page de connexion GRDF a changé. Ouvrez une issue. |
| `Appel API Domoticz impossible … 401` | `127.0.0.1` ne fait pas partie des réseaux de confiance de Domoticz. |
| `Pas de nouvelle donnée GRDF disponible` | GRDF n'a pas encore publié les journées suivantes. C'est normal. |

L'état interne du plugin (dernier jour importé, index) est enregistré dans la configuration du
matériel. Pour forcer une réinitialisation depuis l'historique du périphérique, supprimez le
matériel puis recréez-le.

## Avertissement

GRDF ne propose aucune API publique pour les particuliers. Ce plugin utilise le même fonctionnement que
le site monespace.grdf.fr, et peut cesser de fonctionner si GRDF le modifie. La méthode de connexion
reprend celle de [PyGazpar](https://github.com/ssenart/PyGazpar) : merci à son auteur.

## Licence

[MIT](LICENSE)
