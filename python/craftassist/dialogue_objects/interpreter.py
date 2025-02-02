"""
Copyright (c) Facebook, Inc. and its affiliates.
"""

import logging
import numpy as np
import random
from typing import Tuple, Dict, Any, Optional
from word2number.w2n import word_to_num

from .dialogue_object import DialogueObject, ConfirmTask, Say
from .interpreter_helper import (
    ErrorWithResponse,
    NextDialogueStep,
    get_block_type,
    get_holes,
    get_repeat_arrangement,
    get_repeat_num,
    interpret_location,
    interpret_reference_object,
    interpret_schematic,
    interpret_size,
    interpret_stop_condition,
)
from memory import MobNode
from word_maps import SPAWN_OBJECTS
import dance
import tasks


class Interpreter(DialogueObject):
    """This class handles processes incoming chats and modifies the task stack

    Handlers should add/remove/reorder tasks on the stack, but not execute them.
    """

    def __init__(self, speaker: str, action_dict: Dict, **kwargs):
        super().__init__(**kwargs)
        self.speaker = speaker
        self.action_dict = action_dict
        self.provisional: Dict = {}
        self.action_dict_frozen = False
        self.default_debug_path = "debug_interpreter.txt"
        self.action_handlers = {
            "MOVE": self.handle_move,
            "BUILD": self.handle_build,
            "DESTROY": self.handle_destroy,
            "DIG": self.handle_dig,
            "STOP": self.handle_stop,
            "RESUME": self.handle_resume,
            "FREEBUILD": self.handle_freebuild,
            "UNDO": self.handle_undo,
            "SPAWN": self.handle_spawn,
            "FILL": self.handle_fill,
            "DANCE": self.handle_dance,
        }

    def step(self) -> Tuple[Optional[str], Any]:
        assert self.action_dict["dialogue_type"] == "HUMAN_GIVE_COMMAND"
        try:
            action = self.action_dict["action"]["action_type"]
            response = self.action_handlers[action](self.speaker, self.action_dict["action"])
            return response
        except NextDialogueStep:
            return None, None
        except ErrorWithResponse as err:
            self.finished = True
            return err.chat, None

    def handle_undo(self, speaker, d) -> Tuple[Optional[str], Any]:
        task_name = d.get("undo_action")
        if task_name:
            task_name = task_name.split("_")[0].strip()
        old_task = self.memory.get_last_finished_root_task(task_name)
        if old_task is None:
            raise ErrorWithResponse("Nothing to be undone ...")
        undo_tasks = [tasks.Undo(self.agent, {"memid": old_task.memid})]

        #        undo_tasks = [
        #            tasks.Undo(self.agent, {"memid": task.memid})
        #            for task in old_task.all_descendent_tasks(include_root=True)
        #        ]
        undo_command = old_task.get_chat().chat_text

        logging.info("Pushing ConfirmTask tasks={}".format(undo_tasks))
        self.dialogue_stack.append_new(
            ConfirmTask,
            'Do you want me to undo the command: "{}" ?'.format(undo_command),
            undo_tasks,
        )
        self.finished = True
        return None, None

    def handle_spawn(self, speaker, d) -> Tuple[Optional[str], Any]:
        spawn_obj = d["reference_object"]
        if not spawn_obj:
            raise ErrorWithResponse("I don't understand what you want me to spawn.")

        object_name = self.stemmer.stemWord(spawn_obj["has_name"])
        if object_name in SPAWN_OBJECTS:
            object_idm = (383, SPAWN_OBJECTS[object_name])
        else:
            raise ErrorWithResponse("I don't know how to spawn: %r." % (object_name))

        pos = interpret_location(self, speaker, {"location_type": "SPEAKER_LOOK"})
        repeat_times = get_repeat_num(spawn_obj)
        for i in range(repeat_times):
            task_data = {"object_idm": object_idm, "pos": pos, "action_dict": d}
            self.append_new_task(tasks.Spawn, task_data)
        self.finished = True
        return None, None

    def handle_move(self, speaker, d) -> Tuple[Optional[str], Any]:
        def new_tasks():
            location_d = d.get("location", {"location_type": "SPEAKER_LOOK"})
            pos = interpret_location(self, speaker, location_d)
            if pos is None:
                raise ErrorWithResponse("I don't understand where you want me to move.")
            task_data = {"target": pos, "action_dict": d}
            task = tasks.Move(self.agent, task_data)
            return [task]

        if "stop_condition" in d:
            condition = interpret_stop_condition(self, speaker, d["stop_condition"])
            task_data = {"new_tasks_fn": new_tasks, "stop_condition": condition, "action_dict": d}
            self.append_new_task(tasks.Loop, task_data)
        else:
            for t in new_tasks():
                self.append_new_task(t)

        self.finished = True
        return None, None

    def handle_build(self, speaker, d) -> Tuple[Optional[str], Any]:
        location_d = d.get("location", {"location_type": "SPEAKER_LOOK"})
        origin = interpret_location(self, speaker, location_d)
        # hack to fix build 1 block underground!!! FIXME should SPEAKER_LOOK deal with this?
        if location_d["location_type"] == "SPEAKER_LOOK":
            origin[1] += 1
        if "reference_object" in d:
            # handle copy
            repeat = get_repeat_num(d)
            objs = interpret_reference_object(
                self,
                speaker,
                d["reference_object"],
                limit=repeat,
                ignore_mobs=True,
                loose_speakerlook=True,
            )
            if len(objs) == 0:
                raise ErrorWithResponse("I don't understand what you want me to build")
            tagss = [
                [(p, v) for (_, p, v) in self.memory.get_triples(subj=obj.memid)] for obj in objs
            ]
            interprets = [
                [list(obj.blocks.items()), obj.memid, tags] for (obj, tags) in zip(objs, tagss)
            ]
        else:  # a schematic
            interprets = interpret_schematic(self, speaker, d.get("schematic", {}))

        if len(interprets) > 1:
            offsets = get_repeat_arrangement(
                d, self, speaker, interprets[0][0], repeat_num=len(interprets)
            )
        else:
            offsets = [(0, 0, 0)]

        interprets_with_offsets = [
            (blocks, mem, tags, off) for (blocks, mem, tags), off in zip(interprets, offsets)
        ]

        tasks_todo = []
        for schematic, schematic_memid, tags, offset in interprets_with_offsets:
            og = np.array(origin) + offset

            task_data = {
                "blocks_list": schematic,
                "origin": og,
                "schematic_memid": schematic_memid,
                "schematic_tags": tags,
                "action_dict": d,
            }

            tasks_todo.append(task_data)

        for task_data in reversed(tasks_todo):
            self.append_new_task(tasks.Build, task_data)
        logging.info("Added {} Build tasks to stack".format(len(tasks_todo)))
        self.finished = True
        return None, None

    def handle_freebuild(self, speaker, d) -> Tuple[Optional[str], Any]:
        # This handler handles the action where the agent can complete
        # a human half-built structure using a generative model
        self.dialogue_stack.append_new(Say, "Sorry, I don't know how to do that yet.")
        self.finished = True
        return None, None

    def handle_fill(self, speaker, d) -> Tuple[Optional[str], Any]:
        self.finished = True
        location_d = d.get("location", {"location_type": "SPEAKER_LOOK"})
        location = interpret_location(self, speaker, location_d)
        repeat = get_repeat_num(d)
        holes = get_holes(self, speaker, location, limit=repeat)
        if holes is None:
            self.dialogue_stack.append_new(
                Say, "I don't understand what holes you want me to fill."
            )
            return None, None
        for hole in holes:
            _, hole_info = hole
            poss, hole_idm = hole_info
            fill_idm = get_block_type(d["has_block_type"]) if "has_block_type" in d else hole_idm
            task_data = {"action_dict": d, "schematic": poss, "block_idm": fill_idm}
            self.append_new_task(tasks.Fill, task_data)
        if len(holes) > 1:
            self.dialogue_stack.append_new(Say, "Ok. I'll fill up the holes.")
        else:
            self.dialogue_stack.append_new(Say, "Ok. I'll fill that hole up.")
        self.finished = True
        return None, None

    def handle_destroy(self, speaker, d) -> Tuple[Optional[str], Any]:
        default_ref_d = {"location": {"location_type": "SPEAKER_LOOK"}}
        ref_d = d.get("reference_object", default_ref_d)
        objs = interpret_reference_object(self, speaker, ref_d)
        if len(objs) == 0:
            raise ErrorWithResponse("I don't understand what you want me to destroy.")

        # don't kill mobs
        if all(isinstance(obj, MobNode) for obj in objs):
            raise ErrorWithResponse("I don't kill animals")
        objs = [obj for obj in objs if not isinstance(obj, MobNode)]

        for obj in objs:
            schematic = list(obj.blocks.items())
            task_data = {"schematic": schematic, "action_dict": d}
            self.append_new_task(tasks.Destroy, task_data)
        logging.info("Added {} Destroy tasks to stack".format(len(objs)))
        self.finished = True
        return None, None

    # TODO mark in memory it was stopped by command
    def handle_stop(self, speaker, d) -> Tuple[Optional[str], Any]:
        self.finished = True
        if self.memory.task_stack_pause():
            return "Stopping. What should I do next?", None
        else:
            return "I am not doing anything", None

    # TODO mark in memory it was resumed by command
    def handle_resume(self, speaker, d) -> Tuple[Optional[str], Any]:
        self.finished = True
        if self.memory.task_stack_resume():
            return "resuming", None
        else:
            return "nothing to resume", None

    def handle_dig(self, speaker, d) -> Tuple[Optional[str], Any]:
        def new_tasks():
            location_d = d.get("location", {"location_type": "SPEAKER_LOOK"})
            repeat = get_repeat_num(d)
            origin = interpret_location(self, speaker, location_d)
            attrs = {}
            # set the attributes of the hole to be dug.
            for dim, default in [("depth", 1), ("length", 1), ("width", 1)]:
                key = "has_{}".format(dim)
                if key in d:
                    attrs[dim] = word_to_num(d[key])
                elif "has_size" in d:
                    attrs[dim] = interpret_size(d["has_size"])
                else:
                    attrs[dim] = default

            # add dig tasks in a loop
            z_offset = 0
            tasks_todo = []
            for i in range(repeat):
                og = np.array(origin) + [0, 0, z_offset]  # line them up in +z dir
                t = tasks.Dig(self.agent, {"origin": og, "action_dict": d, **attrs})
                tasks_todo.append(t)
                z_offset += attrs["length"] + 4  # 2-block buffer
            return list(reversed(tasks_todo))

        if "stop_condition" in d:
            condition = interpret_stop_condition(self, speaker, d["stop_condition"])
            self.append_new_task(
                tasks.Loop,
                {"new_tasks_fn": new_tasks, "stop_condition": condition, "action_dict": d},
            )
        else:
            for t in new_tasks():
                self.append_new_task(t)
        self.finished = True
        return None, None

    def handle_dance(self, speaker, d) -> Tuple[Optional[str], Any]:
        def new_tasks():
            location_d = d.get("location")
            repeat = get_repeat_num(d)
            tasks_to_do = []

            if location_d is None:
                dance_fn = random.choice(list(self.memory.dances.values()))
                for i in range(repeat):
                    dance_obj = dance.Movement(self.agent, dance_fn)
                    t = tasks.Dance(self.agent, {"movement": dance_obj})
                    tasks_to_do.append(t)
            else:
                if "coref_resolve" in location_d:
                    dance_fn = random.choice(list(self.memory.dances.values()))
                    pos = interpret_location(self, speaker, location_d)
                    for i in range(repeat):
                        dance_obj = dance.Movement(
                            agent=self.agent, move_fn=dance_fn, dance_location=pos
                        )
                        t = tasks.Dance(self.agent, {"movement": dance_obj})
                        tasks_to_do.append(t)
                elif "reference_object" in location_d:
                    location_reference_object = location_d.get("reference_object")
                    objmems = interpret_reference_object(self, speaker, location_reference_object)
                    if len(objmems) == 0:
                        raise ErrorWithResponse("I don't understand where you want me to go.")
                    else:
                        for i in range(repeat):
                            refmove = dance.RefObjMovement(
                                self.agent,
                                ref_object=objmems[0],
                                relative_direction=location_d["relative_direction"],
                            )
                            t = tasks.Dance(self.agent, {"movement": refmove})
                            tasks_to_do.append(t)
                else:
                    raise ErrorWithResponse("I don't understand where you want me to go.")

            return list(reversed(tasks_to_do))

        if "stop_condition" in d:
            condition = interpret_stop_condition(self, speaker, d["stop_condition"])
            self.append_new_task(
                tasks.Loop,
                {"new_tasks_fn": new_tasks, "stop_condition": condition, "action_dict": d},
            )
        else:
            for t in new_tasks():
                self.append_new_task(t)

        self.finished = True
        return None, None

    def append_new_task(self, cls, data=None):
        # this is badly named, FIXME
        if data is None:
            self.memory.task_stack_push(cls, chat_effect=True)
        else:
            task = cls(self.agent, data)
            self.memory.task_stack_push(task, chat_effect=True)
