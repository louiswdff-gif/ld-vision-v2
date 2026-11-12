# Bot de veille Vinted pour l'achat-revente

Un bot qui surveille en continu les **nouvelles annonces Vinted** correspondant à vos recherches, repère celles
qui sont **revendables avec une marge**, les note (*deal score* de 0 à 100) et vous envoie une **alerte instantanée
sur Discord** (par webhook, ou avec un **bot Discord interactif** : boutons et commandes ; Telegram en option).

> **Le bot détecte et note ; c'est vous qui achetez**, via le lien de l'alerte.
> Il n'achète rien, n'envoie aucun message ni aucune offre aux vendeurs, n'ajoute aucun favori, et n'utilise
> **aucun compte Vinted** (la recherche est publique).

## Sommaire

1. [Comment ça marche](#1-comment-ça-marche)
2. [Installation](#2-installation)
3. [Créer un webhook Discord](#3-créer-un-webhook-discord), le [bot Discord interactif](#bot-discord-interactif-optionnel) et [Telegram](#telegram-optionnel)
4. [Créer une veille depuis une URL de recherche](#4-créer-une-veille-depuis-une-url-de-recherche)
5. [Premiers pas : valider puis lancer](#5-premiers-pas--valider-puis-lancer)
6. [Comprendre une alerte](#6-comprendre-une-alerte)
7. [Suivre vos résultats et régler vos seuils](#7-suivre-vos-résultats-et-régler-vos-seuils)
8. [Lancement en continu (petit VPS ou Raspberry Pi)](#8-lancement-en-continu-petit-vps-ou-raspberry-pi)
9. [Si Vinted bloque](#9-si-vinted-bloque)
10. [Dépannage](#10-dépannage)
11. [Développement et tests](#11-développement-et-tests)

---

## 1. Comment ça marche

Toutes les 60 s (réglable, **jamais moins de 30 s**), pour chaque veille, le bot lit la **page de recherche** de Vinted
(`/catalog?…`, tri « nouveautés d'abord »), comme un visiteur, avec le cookie de session anonyme obtenu en visitant la
page d'accueil. **Une seule requête à la fois**, avec un délai minimal entre deux requêtes.

> **Pourquoi la page et non l'API ?** L'ancienne API `/api/v2/catalog/items` répond désormais « La page n'existe pas ».
> Le site envoie la liste des annonces directement dans sa page : le bot lit ces données (voir
> `bot_vinted/vinted/page_catalogue.py`, le seul fichier à adapter si Vinted change encore). Contrepartie : **une page
> pèse plusieurs Mo** (96 annonces) — espacez les passages (`intervalle_secondes: 120` ou plus) et limitez le nombre de veilles.

Pour chaque nouvelle annonce :

1. le **prix est ajouté à l'historique** (marque + catégorie + taille + état) : c'est la base du prix de revente ;
2. les filtres de la veille s'appliquent (prix, états, mots exclus, liste d'exclusion globale…) ;
3. le bot calcule le **coût réel**, le **prix de revente estimé**, la **marge nette** et le **ROI** ;
4. il détecte les **signaux « pépite »** et calcule le **deal score** (marque, taille, état et nombre de photos
   viennent directement de la liste ; le détail de l'annonce — description, note du vendeur — n'est **pas** chargé,
   voir ci-dessous) ;
5. *(option `reseau.charger_details`, désactivée)* chargement du détail de l'annonce ;
6. **alerte si marge nette ≥ X € ET score ≥ Y** (X et Y se règlent par veille).

Une annonce n'est **jamais alertée deux fois** (identifiant enregistré en base SQLite). Si Discord est en panne,
l'alerte est conservée et renvoyée plus tard.

**Premier lancement d'une veille** : tout l'existant est ignoré, pour ne pas vous inonder. Le bot lit quand même quelques
pages pour **amorcer l'historique de prix** (sans alerter), afin d'estimer correctement les prix de revente dès le départ.
Seules les annonces qui apparaissent **ensuite** déclenchent des alertes.

**Ce que la liste de Vinted ne fournit pas** (et donc ce que le bot ne sait pas, pour l'instant) :
- **la date de publication** : une annonce nouvelle est datée de sa **première détection** (au plus un intervalle après
  sa mise en ligne). Le réglage `premier_lancement_minutes` ne s'applique que si une date de publication est connue ;
- **la description, la note, le nombre d'avis et le pays du vendeur** : les filtres `vendeur_note_min`,
  `vendeur_avis_min` et `vendeur_pays` sont donc **ignorés** (le bot l'indique au démarrage), et le vendeur est
  affiché « Inconnu » dans les alertes. Seul l'indicateur « vendeur professionnel » est connu. La lecture de la page
  de chaque annonce, pour récupérer ces informations, reste à faire.

## 2. Installation

Prérequis : **Python 3.11 ou plus récent**.

```bash
git clone <url-du-depot> bot-vinted && cd bot-vinted

python3 -m venv .venv
source .venv/bin/activate            # Windows : .venv\Scripts\activate
pip install -r requirements.txt

cp config.exemple.yaml config.yaml   # votre configuration (ignorée par git)
cp .env.exemple .env                 # vos secrets (ignoré par git)
chmod 600 .env                       # facultatif mais recommandé
```

- **`config.yaml`** : tous les réglages (aucune valeur n'est codée en dur). Le fichier d'exemple est commenté ligne par ligne.
- **`.env`** : les secrets uniquement (webhooks Discord, token Telegram). `config.yaml` ne contient que le *nom* de
  la variable (par exemple `webhook: DISCORD_WEBHOOK_CARHARTT`), jamais l'URL elle-même.
- **`marques.yaml`** : variantes d'orthographe des marques, pour repérer les marques mal écrites. Ajoutez les vôtres.

Vérifiez votre configuration à tout moment :

```bash
python main.py verifier
```

La commande signale les clés mal orthographiées, les valeurs invalides, les variables manquantes dans `.env`, et
affiche la recherche exacte que le bot extrait de chaque URL.

## 3. Créer un webhook Discord

1. Dans Discord, ouvrez les **paramètres du salon** où vous voulez recevoir les alertes (icône ⚙️ à côté du nom du salon).
2. **Intégrations → Webhooks → Nouveau webhook**.
3. Donnez-lui un nom (par exemple « Veille Vinted »), vérifiez le salon, puis cliquez sur **Copier l'URL du webhook**.
4. Collez l'URL dans `.env` :

   ```env
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/AbCdEf...
   ```

   Cette URL est un **secret** : quiconque la possède peut écrire dans votre salon. Ne la publiez pas ; si elle fuit,
   supprimez le webhook dans Discord et recréez-en un.

**Un salon différent par veille** : créez un autre webhook dans l'autre salon, ajoutez-le dans `.env` sous un nom de
votre choix (`DISCORD_WEBHOOK_CARHARTT=…`), puis référencez ce nom dans la veille :

```yaml
veilles:
  - nom: Carhartt vestes
    webhook: DISCORD_WEBHOOK_CARHARTT
```

Sans `webhook:`, la veille utilise `DISCORD_WEBHOOK_URL`, qui reçoit aussi les messages « bot en pause / reprise ».

### Bot Discord interactif (optionnel)

Au lieu d'un simple webhook, un **vrai bot Discord** poste les alertes et vous laisse piloter le suivi depuis Discord :

- chaque alerte est accompagnée de **boutons** : **🔗 Voir sur Vinted** (un simple *lien* : l'achat reste manuel),
  **🛒 J'ai acheté** (un formulaire s'ouvre, prérempli avec le coût réel estimé, et l'achat est enregistré), puis
  **💰 Vendu** (montant net reçu : la marge réelle est calculée) ;
- des **commandes** : `/top` (meilleures affaires récentes), `/veilles` (réglages et activité de chaque veille), `/etat`
  (actif, en pause avec le motif et l'heure de reprise, ou silencieux), `/stats` (résultats réels), `/acheter` et `/vendu`.

> **Aucun achat automatique** : il n'existe aucun bouton ni commande qui achète, envoie un message ou fait une offre.
> Les boutons « J'ai acheté » et « Vendu » ne font qu'**enregistrer dans votre base locale** ce que vous avez fait vous-même.

**1. Créer le bot** — sur https://discord.com/developers/applications : **New Application** → onglet **Bot** →
**Reset Token** → copiez le token dans `.env` (`DISCORD_BOT_TOKEN=…`). Aucune « intent privilégiée » n'est nécessaire.
Le token est un **secret équivalent à un mot de passe** : s'il fuit, régénérez-le.

**2. L'inviter sur votre serveur** — onglet **OAuth2 → URL Generator** : cochez les scopes **`bot`** et
**`applications.commands`**, et les permissions **View Channels**, **Send Messages** et **Embed Links**. Ouvrez l'URL
générée et choisissez votre serveur.

**3. Récupérer les identifiants** — dans Discord : **Paramètres → Avancés → Mode développeur**. Puis clic droit sur
votre **serveur**, un **salon** et **votre profil** → *Copier l'identifiant*.

**4. Configurer** (`config.yaml`) :

```yaml
discord_bot:
  actif: true
  serveur_id: 111111111111111111          # votre serveur : commandes disponibles tout de suite
  salon_id: 222222222222222222            # salon par défaut des alertes
  utilisateurs_autorises: [333333333333333333]   # vous (et ceux qui peuvent enregistrer achats/ventes)

veilles:
  - nom: Carhartt vestes
    salon_discord: 444444444444444444     # facultatif : un salon dédié à cette veille
```

Quand le bot est actif, **il remplace les webhooks** : seule la variable `DISCORD_BOT_TOKEN` est requise dans `.env`
(Telegram reste utilisable en plus). Lancez normalement : `python main.py` démarre le moteur **et** le bot dans le même
processus (la configuration systemd ne change pas). La connexion est sortante : aucune adresse publique n'est nécessaire.

**Qui peut faire quoi ?** `/top`, `/veilles` et `/etat` sont ouvertes à tous les membres du serveur, les réponses ne
sont visibles que par la personne qui les demande. `/stats`, `/acheter`, `/vendu` et les boutons d'enregistrement sont
**réservés** aux identifiants de `utilisateurs_autorises` ; avec une liste vide, le bot est en lecture seule.

> **À valider chez vous.** Ce bot est couvert par des tests qui simulent Discord (boutons, formulaires, commandes,
> alertes, droits, pannes d'envoi) et la sérialisation des messages a été contrôlée avec la vraie bibliothèque, mais
> il n'a **jamais été connecté à un vrai serveur Discord** depuis l'environnement de développement. Au premier
> lancement, vérifiez que `/etat` répond, puis qu'une alerte apparaît avec ses boutons (essayez `python main.py --une-fois`).

### Telegram (optionnel)

Mêmes alertes, mêmes contenus. Discord reste le canal de référence (une alerte est considérée comme envoyée dès que
Discord l'a reçue).

1. Dans Telegram, parlez à **@BotFather** : `/newbot`, puis suivez les instructions. Il vous donne un **token**.
2. Envoyez un message à votre nouveau bot, puis ouvrez `https://api.telegram.org/bot<TOKEN>/getUpdates` dans un
   navigateur : repérez `"chat":{"id": ...}` — c'est votre **chat id**.
3. Renseignez `.env` : `TELEGRAM_BOT_TOKEN=…` et `TELEGRAM_CHAT_ID=…`.
4. Dans `config.yaml` : `telegram: {actif: true}`.

## 4. Créer une veille depuis une URL de recherche

C'est la méthode la plus simple : **collez l'URL d'une recherche Vinted**, le bot en extrait tout seul les critères.

1. Sur vinted.fr, faites votre recherche avec tous les filtres voulus (mots-clés, marque, catégorie, taille, couleur,
   matière, état, prix…).
2. Copiez l'**URL de la barre d'adresse** :
   `https://www.vinted.fr/catalog?search_text=carhartt%20veste&brand_ids[]=362&status_ids[]=6&price_to=40&order=newest_first`
3. Ajoutez une veille dans `config.yaml` :

   ```yaml
   veilles:
     - nom: Carhartt vestes
       url_recherche: "https://www.vinted.fr/catalog?search_text=carhartt%20veste&order=newest_first"
       prix_max: 40
       etats: [neuf_avec_etiquette, neuf_sans_etiquette, tres_bon_etat]
       mots_exclus: [enfant]
       vendeur_note_min: 4.5
       vendeur_avis_min: 5
       port_estime: 6.50          # frais de port estimés pour cette veille
       prix_revente_cible: 70     # prix de revente utilisé tant que l'historique est trop court
       marge_min: 20              # alerte seulement si marge nette ≥ 20 €…
       score_min: 60              # …ET deal score ≥ 60
       webhook: DISCORD_WEBHOOK_CARHARTT
   ```

4. Lancez `python main.py verifier` : la ligne « recherche » montre ce que le bot a compris de l'URL.

**Ce que le bot extrait de l'URL** : mots-clés (`search_text`), marques (`brand_ids`), catégories (`catalog[]` ou
l'identifiant dans le chemin `/catalog/2050-…`), tailles, couleurs, matières, états (`status_ids`), prix min et max.
Les paramètres qu'il ne gère pas sont listés en avertissement. **Les champs écrits dans le YAML l'emportent sur
l'URL** (ici `prix_max: 40` remplace le `price_to` éventuel de l'URL).

Sans URL, décrivez la recherche directement : `mots_cles: [ralph lauren, pull]`, `marques: [362]`,
`categories: [2050]`, `tailles: [...]`, `couleurs: [...]`, `matieres: [...]` (identifiants Vinted).

### Filtres disponibles par veille

| Clé | Rôle |
|---|---|
| `mots_cles`, `mots_exclus` | texte recherché ; mots qui écartent une annonce |
| `marques`, `categories`, `tailles`, `couleurs`, `matieres` | identifiants Vinted (envoyés à Vinted) |
| `etats` | `neuf_avec_etiquette`, `neuf_sans_etiquette`, `tres_bon_etat`, `bon_etat`, `satisfaisant` |
| `prix_min`, `prix_max` | fourchette de prix de l'article |
| `vendeur_note_min`, `vendeur_avis_min`, `vendeur_pays` | fiabilité et pays du vendeur (`[FR, BE]`) |
| `photos_min` | nombre minimal de photos |
| `marque_stricte` | `true` par défaut : si la recherche contient une marque connue (`marques.yaml`), n'alerte que sur cette marque (une marque absente du champ mais présente, même mal écrite, dans le titre est acceptée). La recherche de Vinted renvoie aussi d'autres marques, auxquelles votre prix de revente cible ne s'applique pas. |
| `exceptions_exclusion` | mots de la liste d'exclusion globale autorisés pour cette veille |
| `intervalle_secondes` | cadence de cette veille (minimum 30) |
| `domaine` | `vinted.fr` par défaut ; `vinted.be`, `vinted.es`, `vinted.it`… (déduit de l'URL sinon) |
| `variantes_marque: true` | cherche aussi les variantes de la marque (voir ci-dessous) |

> **Pourquoi le prix et l'état ne sont pas envoyés à Vinted ?** Le bot applique `prix_max` et `etats` lui-même, *après*
> avoir enregistré chaque annonce dans l'historique. Sinon l'historique ne contiendrait que des annonces bon marché
> et le prix de revente serait systématiquement sous-estimé.

**Liste d'exclusion globale** (`exclusions_globales`) : contrefaçons (`type`, `style`, `inspired`, `replica`,
`réplique`, `copie`, `fake`, `dupe`) et défauts (`trou`, `tache`, `abîmé`, `déchiré`). Un mot est reconnu comme **mot
entier** (« trou » ne bloque pas « trouvé »), sans tenir compte des accents ni des pluriels. Ces annonces sont aussi
écartées de l'historique de prix. Une veille peut autoriser une exception : `exceptions_exclusion: [style]`.

### Surveiller plein de marques tendance

`config.tendance.exemple.yaml` est une configuration prête à l'emploi : **14 veilles** sur des marques qui se revendent
bien et sont peu contrefaites en occasion (Ralph Lauren, Carhartt, Patagonia, Barbour, Levi's 501, Dickies, Fred Perry,
Helly Hansen, Gant, Saint James). Copiez-la en `config.yaml`, puis `python main.py verifier`.

- **Le choix des marques est une connaissance générale du marché, pas une mesure du bot** : le bot ne sait ni ce qui se
  vend vite, ni ce qui est contrefait. Les marques très contrefaites (Stone Island, CP Company, Burberry, Moncler,
  Supreme, Stüssy, Gucci, Louis Vuitton, sneakers) sont volontairement absentes. Éditez la liste selon votre expérience.
- **Aucun prix de revente n'est inventé** : sans `prix_revente_cible`, le bot estime la revente à partir des prix
  demandés qu'il observe et n'alerte qu'avec **10 références** (même marque et même état ; la taille exacte d'abord,
  puis « toutes tailles »). Comptez **quelques heures** avant les premières alertes ; ajoutez un `prix_revente_cible`
  réaliste pour alerter plus tôt.
- **L'historique est propre à chaque veille** (un polo ne sert pas à valoriser un pull) : si vous renommez une veille,
  son historique repart de zéro. Une annonce qui correspond à deux veilles n'est comptée que dans la première qui la voit.
- **Le trafic augmente avec le nombre de veilles** (une page de plusieurs Mo par veille et par passage) : retirez celles
  qui ne vous servent pas ou allongez `intervalle_secondes`.
- Un mot numérique (`501`) est accepté dans `mots_cles` sans guillemets.

## 5. Premiers pas : valider puis lancer

**a) Validez le format réel de Vinted (recommandé, une fois).** Le lecteur de page a été écrit d'après la structure
réelle d'une page de recherche observée depuis un navigateur, mais Vinted la modifie parfois. Faites **une** vraie
requête depuis votre propre connexion et regardez ce que le bot comprend :

```bash
python main.py capturer "https://www.vinted.fr/catalog?search_text=carhartt%20veste" reponse_reelle.json
```

La commande lit la page de recherche, enregistre la liste d'annonces qu'elle contient (sans cookies ni identifiants
de session), indique combien d'annonces le lecteur a comprises et signale les champs qu'il n'a pas su extraire. Si
quelque chose manque, **`bot_vinted/vinted/page_catalogue.py` est le seul fichier à adapter**. Vous pouvez copier le
fichier obtenu dans `tests/fixtures/` pour en faire un test de non-régression.

**b) Essayez en mode simulation** : les alertes s'affichent dans la console, **rien n'est envoyé**.

```bash
python main.py --dry-run
```

> Le mode simulation enregistre l'historique et les annonces « déjà vues » comme un vrai passage : après un
> `--dry-run`, les annonces déjà affichées ne seront pas renvoyées en mode réel. Pour repartir de zéro, supprimez
> le fichier `donnees/bot_vinted.db`.

**c) Lancez pour de vrai** :

```bash
python main.py                  # surveillance en continu (Ctrl+C pour arrêter proprement)
python main.py --une-fois       # un seul passage par veille, puis arrêt
python main.py -v               # logs détaillés (DEBUG) : voir pourquoi chaque annonce est écartée
```

Les logs sont lisibles en console et écrits dans `logs/bot_vinted.log` (fichier rotatif : 5 Mo × 3 par défaut).
Les webhooks et tokens y sont automatiquement masqués.

## 6. Comprendre une alerte

Chaque annonce retenue donne **un embed Discord** : photo, titre, marque, taille, état, prix affiché, **coût réel**,
**prix de revente estimé**, **marge nette**, **ROI**, **deal score**, signaux, note et nombre d'avis du vendeur,
ancienneté de l'annonce et lien direct (avec le bot interactif : les boutons *Voir sur Vinted*, *J'ai acheté*, *Vendu*).

**Calculs**

- **Coût réel** = prix + frais de protection acheteur (0,70 € + 5 % du prix, paramétrables) + frais de port estimés de la veille.
- **Prix de revente** = **médiane** des prix demandés vus pour la même combinaison marque + catégorie + taille + état,
  multipliée par une **décote** (0,85 par défaut : ce sont des prix demandés, pas des prix vendus). Avec **moins de
  10 annonces de référence**, le bot utilise le `prix_revente_cible` de la veille.
- **Marge nette** = prix de revente estimé − coût réel ; **ROI** = marge ÷ coût réel.
- **Lot** (« lot de 3 pulls ») : valeur unitaire × quantité × décote de lot (0,90).

**Deal score (0-100)** : moyenne pondérée de l'écart à la médiane (30 %), de la marge en € (25 %), du ROI (20 %), de
l'état (10 %), de la fraîcheur (10 %) et de la fiabilité du vendeur (5 %), plus un bonus « pépite » plafonné.
Tous les poids sont dans `config.yaml` (`score:`). **🔥 à partir de 80, ✅ de 60 à 79.**

**Signaux « pépite »** (bonus de score + étiquette dans l'alerte) :

- 🔤 **marque mal orthographiée** (« carhart », « ralf lauren »…) ou 🏷️ **absente du champ marque** mais présente dans le
  titre ou la description : variantes listées dans `marques.yaml` + *fuzzy matching* pour les fautes non listées.
  Un champ marque renseigné fait foi : « type Carhartt » dans le titre d'un Nike n'est pas une pépite Carhartt.
  *La recherche de Vinted ne retrouve pas toujours une annonce dont le titre contient une faute (chercher « carhartt »
  ne renvoie pas forcément « carhart »). Pour les attraper, activez `variantes_marque: true` sur la veille : le bot
  lance aussi des recherches avec les variantes de la marque (sans filtre de marque). Cela ajoute des requêtes.*
- 🔍 **annonce mal mise en valeur** : titre vague (sans marque ni modèle) ou une à deux photos seulement ;
- ⏱️ **vente pressée** : « urgent », « déménagement », « vide dressing », « à saisir »… ;
- 📦 **lot** dont la valeur à l'unité peut être estimée.

## 7. Suivre vos résultats et régler vos seuils

```bash
python main.py acheter 1234567890 36.10     # ce que vous avez réellement payé, TOUT compris
python main.py vendu   1234567890 70        # le montant NET que vous avez reçu
python main.py stats
```

- `acheter <id> <prix>` : `<prix>` = **total payé, tout compris** (article + protection acheteur + port), tel qu'il
  figure sur votre reçu. On peut donner l'**URL de l'annonce** à la place de l'identifiant. Si le bot avait alerté sur
  cette annonce, il rattache la veille et la marge qu'il avait estimée.
- `vendu <id> <prix>` : `<prix>` = **montant net reçu**. Le bot affiche la marge réelle, le ROI et le délai de revente.
- `stats` : marge réelle, ROI moyen, délai moyen de revente, **veilles les plus rentables**, et la comparaison
  **marge estimée / marge réelle** avec un conseil : si le bot surestime vos marges, baissez `revente.decote` ou
  relevez `marge_min` / `score_min` ; s'il les sous-estime, vous pouvez assouplir vos seuils.

## 8. Lancement en continu (petit VPS ou Raspberry Pi)

Le bot consomme très peu de ressources : un Raspberry Pi ou un VPS d'entrée de gamme suffit.

> **Conseil sur l'IP.** Vinted bloque plus volontiers les adresses de centres de données (beaucoup de VPS) que les
> connexions résidentielles. Un **Raspberry Pi chez vous** est souvent plus fiable. Le bot ne contourne aucune
> protection (pas de proxy, pas de rotation d'IP) : si Vinted bloque votre IP, il se met en pause et vous prévient
> (voir [Si Vinted bloque](#9-si-vinted-bloque)).

### Avec systemd (recommandé)

```bash
# 1. Un utilisateur dédié et le code dans /opt/bot-vinted
sudo useradd --system --create-home --shell /usr/sbin/nologin vinted
sudo mkdir -p /opt/bot-vinted && sudo chown vinted: /opt/bot-vinted
sudo -u vinted git clone <url-du-depot> /opt/bot-vinted
cd /opt/bot-vinted

# 2. Environnement Python (Raspberry Pi OS « Bookworm » fournit Python 3.11 ; sinon : sudo apt install python3-venv)
sudo -u vinted python3 -m venv .venv
sudo -u vinted .venv/bin/pip install -r requirements.txt

# 3. Configuration et secrets
sudo -u vinted cp config.exemple.yaml config.yaml    # puis éditez-le
sudo -u vinted cp .env.exemple .env                  # puis renseignez vos secrets
sudo chmod 600 /opt/bot-vinted/.env
sudo -u vinted .venv/bin/python main.py verifier

# 4. Service (fichier fourni : deploy/bot-vinted.service)
sudo cp deploy/bot-vinted.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bot-vinted
```

Commandes utiles :

```bash
sudo systemctl status bot-vinted          # état
journalctl -u bot-vinted -f               # logs en direct
tail -f /opt/bot-vinted/logs/bot_vinted.log
sudo systemctl restart bot-vinted         # après modification de config.yaml
sudo -u vinted /opt/bot-vinted/.venv/bin/python main.py stats    # vos résultats
```

Pour mettre à jour : `cd /opt/bot-vinted && sudo -u vinted git pull && sudo -u vinted .venv/bin/pip install -r requirements.txt && sudo systemctl restart bot-vinted`.

À l'arrêt (`systemctl stop` ou Ctrl+C), le bot termine la requête en cours puis s'arrête proprement.

### Sans systemd (solution simple)

```bash
sudo apt install tmux
tmux new -s vinted
source .venv/bin/activate && python main.py
# détacher : Ctrl+B puis D ; revenir : tmux attach -t vinted
```

## 9. Si Vinted bloque

Le bot est conçu pour **rester discret** : une requête à la fois, cadence lente, aucun contournement (pas de
résolution de CAPTCHA, pas d'imitation d'empreinte de navigateur, pas de proxy ni de rotation d'IP).

En cas de **429** (trop de requêtes), de **403** persistant (accès refusé), de **page de vérification anti-bot**, ou de
**réponse inattendue** (404 répété, format de réponse modifié, détails d'annonces « introuvables » d'affilée) :

1. un 401/403 déclenche d'abord **une seule** nouvelle tentative avec un cookie de session neuf ;
2. si le blocage persiste, le bot **ralentit** : backoff exponentiel (60 s, 120 s, 240 s… jusqu'à 30 min, réglable dans
   `reseau.backoff`), en respectant l'en-tête `Retry-After` du serveur ;
3. au deuxième échec consécutif, il envoie **un seul message « bot en pause »** sur Discord (pas de spam) ;
4. dès que Vinted répond de nouveau, il reprend tout seul et envoie **un message « reprise »**.

Aucune annonce n'est perdue : une annonce en cours d'examen au moment du blocage est réexaminée après la pause.
Si les blocages sont fréquents, augmentez `intervalle_secondes`, réduisez le nombre de veilles ou changez de réseau
(une connexion résidentielle).

> **À savoir** : les conditions d'utilisation de Vinted restreignent l'extraction automatisée de données. Ce bot
> n'utilise aucun compte (donc aucun compte ne peut être sanctionné) et reste volontairement lent, mais l'usage
> relève de votre responsabilité.

## 10. Dépannage

| Symptôme | Explication et solution |
|---|---|
| **Aucune alerte** après le démarrage | Normal au premier passage : l'existant est ignoré (annonces de plus de 15 min). Patientez, ou lancez `-v` pour voir pourquoi chaque annonce est écartée (`prix_max`, `marge`, `score`, `mot_exclu`…). |
| Beaucoup d'annonces écartées pour « marge » | `prix_revente_cible` trop bas, `marge_min` trop haut, ou port estimé trop élevé. Avec moins de 10 références, c'est `prix_revente_cible` qui sert de base. |
| Annonces « non_estimable » | Ni 10 références, ni `prix_revente_cible` dans la veille : ajoutez un prix cible. |
| « Variables d'environnement manquantes » | Renseignez `.env` (noms exacts de `config.yaml`), ou testez avec `--dry-run`. |
| Message « bot en pause » | Voir [Si Vinted bloque](#9-si-vinted-bloque). Le bot reprend seul. |
| Pause avec « réponse inattendue », « sans liste `items` » ou « 404 » | Soit Vinted bloque votre adresse IP (très fréquent depuis un VPS), soit son format a changé. Lancez `python main.py capturer "<url de recherche>" test.json` depuis la même machine : si la commande est refusée, c'est l'IP ; si elle réussit mais que le parseur signale des champs manquants, adaptez `bot_vinted/vinted/page_catalogue.py`. |
| « Filtres vendeur IGNORÉS » au démarrage | Normal : la note, le nombre d'avis et le pays du vendeur ne figurent pas dans la liste de Vinted (voir « Premier lancement… » plus haut). |
| Libellé d'état inconnu dans les logs | Un site Vinted dans une autre langue : ajoutez le libellé dans `bot_vinted/vinted/constantes.py` (`LIBELLES_ETATS`). |
| Les commandes `/…` n'apparaissent pas | Le bot a été invité sans le scope **`applications.commands`** : régénérez l'URL d'invitation (étape 2) et réinvitez-le. Renseignez aussi `discord_bot.serveur_id` (sinon Discord peut mettre jusqu'à une heure à les afficher). |
| « Discord a refusé le token du bot » | Le `DISCORD_BOT_TOKEN` de `.env` est faux ou a été régénéré. |
| « salon … introuvable ou inaccessible » / « permission refusée » | Mauvais identifiant de salon, ou le bot n'a pas accès à ce salon : donnez-lui **View Channel**, **Send Messages** et **Embed Links** dessus. Les alertes sont conservées et renvoyées dès que c'est corrigé. |
| « Vous n'êtes pas autorisé… » sur un bouton | Votre identifiant Discord n'est pas dans `discord_bot.utilisateurs_autorises`. |
| Rien ne s'affiche dans Discord mais pas d'erreur | Vérifiez que le webhook n'a pas été supprimé (le log indique « Discord a refusé l'envoi (HTTP 404) »). Les alertes non envoyées sont conservées et renvoyées. |
| Les annonces d'un domaine hors zone euro | Les frais et marges sont exprimés dans la devise du site ; adaptez `frais_acheteur` en conséquence. |

## 11. Développement et tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Les tests s'exécutent **sur des réponses JSON enregistrées** (`tests/fixtures/`) et sur un faux Vinted simulé :
**aucun test n'appelle le vrai Vinted**. Le test de bout en bout (`tests/test_moteur_e2e.py`) rejoue des passages
complets : premier lancement, dédoublonnage, filtres, pause sur 429, reprise, renvoi d'alertes, dry-run.

```
main.py                    point d'entrée
config.exemple.yaml        configuration commentée (à copier en config.yaml)
marques.yaml               variantes d'orthographe des marques
bot_vinted/
  config.py                configuration (pydantic) et secrets (.env)
  journal.py               logs console + fichier rotatif, masquage des secrets
  base.py                  SQLite : annonces vues, historique de prix, alertes, achats
  moteur.py                planificateur et pipeline d'un passage
  filtres.py               filtres de veille et liste d'exclusion globale
  marques.py               variantes + fuzzy matching (rapidfuzz)
  valorisation.py          coût réel, médiane, prix de revente, marge, ROI
  signaux.py               pépites : lots, vente pressée, annonce mal valorisée
  score.py                 deal score 0-100
  evaluation.py            assemblage valorisation + signaux + score
  suivi.py / capture.py    commandes acheter · vendu · stats / capturer
  vinted/                  url_recherche · client (cookie, cadence, backoff) · page_catalogue (lecture de la page) · modeles · pause
  alertes/                 console (--dry-run) · discord (webhook) · telegram
  discord_bot/             bot Discord interactif : logique (embeds, règles) · bot (discord.py, boutons, commandes)
tests/                     fixtures JSON et tests
deploy/bot-vinted.service  exemple de service systemd
```
