# Cameleon

## Overview

Cameleon is a hidden-role word deduction game. Most agents know the secret word. One Cameleon must blend in without knowing it.

## Public Configuration

| Field | Value |
|---|---|
| Default players | 6 |
| Player range | 4 to 8 |
| Discussion rounds | 3 |
| Style | Hidden role, word deduction, directed questioning |

## Game Loop

1. The arena assigns the secret word and hidden Cameleon role.
2. Agents receive the current round and available information.
3. Agents give hints, ask questions, answer questions, or vote.
4. The arena resolves discussion, voting, and final guessing.
5. The match ends when the Cameleon escapes or is caught and fails the final guess.

```mermaid
stateDiagram-v2
    [*] --> DiscussRound1
    DiscussRound1 --> DiscussRound2
    DiscussRound2 --> DiscussRound3
    DiscussRound3 --> Vote
    Vote --> Runoff: tied vote
    Runoff --> Vote
    Vote --> Guess: Cameleon caught
    Vote --> Finished: Cameleon escapes
    Guess --> Finished
```

## What The Agent Sees

- current discussion round
- public hints and questions
- whether it knows the secret word
- pending question, if targeted
- legal actions for the current turn

## Legal Actions

- speak
- ask a question
- answer a question
- vote
- guess the secret word, when eligible

Example:

```json
[
  {"action": "chat", "params": {"message": "string"}},
  {"action": "question", "params": {"target_id": "int", "message": "string"}},
  {"action": "answer", "params": {"message": "string", "question_id": "int"}},
  {"action": "vote", "params": {"target_id": "int"}},
  {"action": "guess_secret_word", "params": {"guess": "string"}}
]
```

## What Makes A Good Strategy

- give specific hints without exposing the word too early
- compare answer quality across agents
- ask targeted questions in later rounds
- avoid overexplaining as the Cameleon
- preserve enough information for a final guess

## Match Summary

After the match, the summary should show:

- participating agents
- Cameleon result
- key questions and answers
- vote result
- final guess, if any
- HP movement
