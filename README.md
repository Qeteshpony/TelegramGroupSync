### A simple bot that can sync members of two Telegram groups

If you have one group with member management and you want a subgroup for another topic without effort this is the
right bot for you. Hand out an approval link to the secondary group and the bot will automatically approve all
requests from people who are in the primary group while denying everyone else.

If anyone leaves the primary group they get automatically removed from the secondary as well if they're in there.
The same check runs when someone gets into the secondary group another way, for example added by an admin.

Restrictions (like a mute) set for a member in the primary group are applied in the secondary group too and lifted
again when they are lifted in the primary group. Restrictions set by hand in the secondary group are kept.

### Setup

- Both groups must be supergroups.
- The bot must be admin in the primary group, otherwise it gets no member updates.
- In the secondary group the bot must be admin with the rights to invite users and to ban users (banning includes
  restricting).
- Copy `example.env` to `.env`, fill in the values and run `docker compose up -d --build`.

The bot remembers which restrictions it set itself in a state file, kept in the Docker volume `state`. Without
Docker the file is `state.json` in the working directory, or the path given in `STATE_FILE`.

### Limits

- The bot never removes or declines anyone when Telegram cannot be reached or gives an unclear answer. Join requests
  stay pending in that case and can be handled by an admin.
- Telegram bots cannot list the members of a group, so the bot cannot go through the secondary group and remove
  everyone who is not in the primary group. It only reacts to changes that happen while it is set up.
- Telegram keeps updates for about 24 hours. Changes that happen while the bot is offline for longer are missed.
- If the state file is lost, restrictions the bot set in the secondary group are no longer lifted automatically.
- Admins and the owner of the secondary group cannot be removed or restricted by the bot.
