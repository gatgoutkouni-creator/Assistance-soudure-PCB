# Assistance Soudure PCB

**Étude et implémentation d'une solution de soudure semi-automatique de cartes électroniques**

Projet de Fin d'Études (PFE) réalisé par **Gatgout Kouni** et **Wedad Louleid**.


## Présentation

Ce projet consiste à concevoir et réaliser une solution d'assistance à la soudure des cartes électroniques (PCB). L'objectif est de faciliter les opérations de soudure en améliorant le positionnement de l'outil et en réduisant les interventions manuelles de l'opérateur.

La solution développée permet d'exploiter les données issues des fichiers Gerber afin d'identifier les positions nécessaires aux opérations de soudure. Ces données sont ensuite traitées et utilisées pour générer les commandes permettant de piloter la machine.

Le système repose sur une interface web permettant à l'utilisateur d'importer les fichiers et de contrôler les différentes étapes du processus. Un serveur développé en Python avec Flask assure le traitement des données et la communication avec le système de commande.

La partie commande de la machine est basée sur une carte Arduino UNO associée à un CNC Shield et à des drivers DRV8825. Le firmware GRBL permet d'interpréter les commandes G-code et de commander les mouvements des axes X, Y et Z.

Une caméra est également utilisée pour assurer la supervision visuelle du système et faciliter le contrôle des opérations.

## Fonctionnement

Le fonctionnement général du système peut être résumé comme suit :

1. Importation du fichier Gerber à travers le dashboard web.
2. Traitement des données du fichier.
3. Extraction des positions nécessaires à la soudure.
4. Validation des points à traiter.
5. Génération des commandes G-code.
6. Envoi des commandes vers le système de commande.
7. Interprétation des commandes par GRBL.
8. Déplacement des axes X, Y et Z.
9. Réalisation des opérations de soudure aux positions définies.
10. Supervision du processus à travers l'interface web et la caméra.

## Architecture du système

Le système est organisé autour de plusieurs parties complémentaires :

**Interface Web → Serveur Python/Flask → Traitement des données → G-code → GRBL → Arduino UNO → CNC Shield + DRV8825 → Axes X/Y/Z**

La caméra permet également de suivre visuellement le fonctionnement de la machine.

## Technologies utilisées

### Logiciel

- Python
- Flask
- HTML / CSS / JavaScript
- G-code
- GRBL

### Commande

- Arduino UNO
- CNC Shield
- DRV8825
- Moteurs pas à pas

### Données et supervision

- Fichiers Gerber
- Dashboard web
- Caméra

## Objectifs du projet

Le système a été développé afin de :

- améliorer la précision du positionnement ;
- faciliter les opérations de soudure ;
- réduire les interventions manuelles ;
- améliorer la répétabilité des opérations ;
- permettre une supervision plus simple du processus.

## Résultats

La solution réalisée permet de traiter les données nécessaires aux opérations de soudure et de commander les déplacements de la machine à partir des positions définies.

Les essais réalisés ont permis de vérifier le fonctionnement de la chaîne de traitement, depuis l'importation des données jusqu'au pilotage des axes de la machine.


## Démonstration vidéo

<video src="videos/demo_gravure_pcb.mp4" controls width="600"></video>

Si la vidéo ne s'affiche pas directement, tu peux la télécharger ou la visionner ici : [videos/demo_gravure_pcb.mp4](videos/demo_gravure_pcb.mp4)

## Aperçu du projet

**De la conception (SolidWorks) à la machine réelle :**

![Conception vs réalisation](captures/conception_vs_realisation.png)

**Tableau de bord de contrôle de la machine :**

![Dashboard de contrôle](captures/dashboard_controle.png)

## Contexte

Projet de Fin d'Études réalisé au sein de **emkaMED**, Ouardanine, Monastir, Tunisie.


## Auteurs

- **Gatgout Kouni**
- **Wedad Louleid**
