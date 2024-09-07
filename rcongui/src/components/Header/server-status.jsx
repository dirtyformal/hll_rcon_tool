import React, { useState, useEffect, useRef } from "react";
import "react-toastify/dist/ReactToastify.css";
import { get, handle_http_errors, showResponse } from "../../utils/fetchUtils";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Link from "@mui/material/Link";
import { fromJS, List } from "immutable";
import { IconButton, Typography } from "@mui/material";
import SwapVertIcon from '@mui/icons-material/SwapVert';
import { styled } from "@mui/material/styles";

const Wrapper = styled('div')(({ theme }) => ({
  paddingLeft: theme.spacing(1),
}));

const MenuBox = styled('div')(({ theme }) => ({
  display: "flex",
  gap: theme.spacing(1),
}));

const ServerStatus = () => {
  const [name, setName] = useState("");
  const [numCurrentPlayers, setNumCurrentPlayers] = useState(0);
  const [maxPlayers, setMaxPlayers] = useState(0);
  const [map, setMap] = useState(null);
  const [timeRemaining, setTimeRemaining] = useState("0:00:00");
  const [balance, setBalance] = useState("0vs0");
  const [score, setScore] = useState("0:0");
  const intervalRef = useRef(null);

  const refreshInterval = 15 * 1000;

  const load = async () => {
    return get(`get_status`)
      .then((response) => showResponse(response, "get_status", false))
      .then((data) => {
        setName(data?.result.name);
        setMap(data?.result.map);
        setNumCurrentPlayers(data.result.current_players);
        setMaxPlayers(data.result.max_players);
        document.title = `(${data?.result.current_players}) ${data?.result.short_name}`;
      })
      .catch((error) => { clearInterval(intervalRef.current) });
  };

  const loadInfo = async () => {
    return get(`get_gamestate`)
      .then((response) => showResponse(response, "get_gamestate", false))
      .then((data) => {
        setBalance(
          `${data.result.num_allied_players}vs${data.result.num_axis_players}`
        );
        setScore(`${data.result.allied_score}:${data.result.axis_score}`);
        setTimeRemaining(data.result.raw_time_remaining);
      })
      .catch((error) => { clearInterval(intervalRef.current) });
  };

  useEffect(() => {
    load();
    loadInfo();

    intervalRef.current = setInterval(() => {
      load();
      loadInfo();
    }, refreshInterval);

    return () => {
      clearInterval(intervalRef.current);
    };
  }, []);

  return (
    (<Wrapper>
      <MenuBox>
        <Typography variant="subtitle2" component={"span"} color="inherit">
          {name}
        </Typography>
      </MenuBox>
      <Typography variant="caption">
        {numCurrentPlayers}/{maxPlayers} ({balance}) -{" "}
        {map?.pretty_name ?? "Unknown Map"} - {timeRemaining} - {score}
      </Typography>
    </Wrapper>)
  );
};

export default ServerStatus;
